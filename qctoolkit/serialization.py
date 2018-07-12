""" This module provides serialization and storage functionality.

Classes:
    - StorageBackend: Abstract representation of a data storage.
    - FilesystemBackend: Implementation of a file system data storage.
    - ZipFileBackend: Like FilesystemBackend but inside a single zip file instead of a directory
    - CachingBackend: A caching decorator for StorageBackends.
    - Serializable: An interface for serializable objects.
    - Serializer: Converts Serializables to a serial representation as a string and vice-versa.
"""

from abc import ABCMeta, abstractmethod
from typing import Dict, Any, Optional, NamedTuple, Union, Mapping, MutableMapping
import os
import zipfile
import tempfile
import json
import weakref
import warnings
import gc
from contextlib import contextmanager

from qctoolkit.utils.types import DocStringABCMeta

__all__ = ["StorageBackend", "FilesystemBackend", "ZipFileBackend", "CachingBackend", "Serializable", "Serializer",
           "AnonymousSerializable", "DictBackend", "JSONSerializableEncoder", "JSONSerializableDecoder", "PulseStorage"]


class StorageBackend(metaclass=ABCMeta):
    """A backend to store data/files in.

    Used as an abstraction of file systems/databases for the serializer.
    """

    @abstractmethod
    def put(self, identifier: str, data: str, overwrite: bool=False) -> None:
        """Store the data string identified by identifier.

        Args:
            identifier (str): A unique identifier/name for the data to be stored.
            data (str): A serialized string of data to be stored.
            overwrite (bool): Set to True, if already existing data shall be overwritten.
                (default: False)
        Raises:
            FileExistsError if overwrite is False and there already exists data which
                is associated with the given identifier.
        """

    def __setitem__(self, identifier: str, data: str) -> None:
        self.put(identifier, data)

    @abstractmethod
    def get(self, identifier: str) -> str:
        """Retrieve the data string with the given identifier.

        Args:
            identifier (str): The identifier of the data to be retrieved.
        Returns:
            A serialized string of the data associated with the given identifier, if present.
        Raises:
            KeyError if no data is associated with the given identifier.
        """

    def __getitem__(self, identifier: str) -> str:
        return self.get(identifier)

    @abstractmethod
    def exists(self, identifier: str) -> bool:
        """Check if data is stored for the given identifier.

        Args:
            identifier (str): The identifier for which presence of data shall be checked.
        Returns:
            True, if stored data is associated with the given identifier.
        """

    def __contains__(self, identifier: str) -> bool:
        return self.exists(identifier)

    @abstractmethod
    def delete(self, identifier: str) -> None:
        """Delete a data of the given identifier.

        Args:
            identifier: identifier of the data to be deleted

        Raises:
            KeyError if there is no data associated with the identifier
        """

    def __delitem__(self, identifier: str) -> None:
        self.delete(identifier)


class FilesystemBackend(StorageBackend):
    """A StorageBackend implementation based on a regular filesystem.

    Data will be stored in plain text files in a directory. The directory is given in the
    constructor of this FilesystemBackend. For each data item, a separate file is created an named
    after the corresponding identifier.
    """

    def __init__(self, root: str='.') -> None:
        """Create a new FilesystemBackend.

        Args:
            root: The path of the directory in which all data files are located. (default: ".",
                i.e. the current directory)
        Raises:
            NotADirectoryError: if root is not a valid directory path.
        """
        if not os.path.isdir(root):
            raise NotADirectoryError()
        self._root = os.path.abspath(root)

    def _path(self, identifier) -> str:
        return os.path.join(self._root, identifier + '.json')

    def put(self, identifier: str, data: str, overwrite: bool=False) -> None:
        if self.exists(identifier) and not overwrite:
            raise FileExistsError(identifier)
        path = self._path(identifier)
        with open(path, 'w') as file:
            file.write(data)

    def get(self, identifier: str) -> str:
        path = self._path(identifier)
        try:
            with open(path) as file:
                return file.read()
        except FileNotFoundError as fnf:
            raise KeyError(identifier) from fnf

    def exists(self, identifier: str) -> bool:
        path = self._path(identifier)
        return os.path.isfile(path)

    def delete(self, identifier):
        try:
            os.remove(self._path(identifier))
        except FileNotFoundError as fnf:
            raise KeyError(identifier) from fnf


class ZipFileBackend(StorageBackend):
    """A StorageBackend implementation based on a single zip file.

    Data will be stored in plain text files inside a zip file. The zip file is given
    in the constructor of this FilesystemBackend. For each data item, a separate
    file is created and named after the corresponding identifier.

    ZipFileBackend uses significantly less storage space and is faster on
    network devices, but takes longer to update because every write causes a
    complete recompression (it's not too bad)."""

    def __init__(self, root: str='./storage.zip') -> None:
        """Create a new FilesystemBackend.

        Args:
            root (str): The path of the zip file in which all data files are stored. (default: "./storage.zip",
                i.e. the current directory)
        Raises:
            NotADirectoryError if root is not a valid path.
        """
        parent, fname = os.path.split(root)
        if not os.path.isdir(parent):
            raise NotADirectoryError()
        if not os.path.isfile(root):
            z = zipfile.ZipFile(root, "w")
            z.close()
        self._root = root

    def _path(self, identifier) -> str:
        return os.path.join(identifier + '.json')

    def put(self, identifier: str, data: str, overwrite: bool=False) -> None:
        if not self.exists(identifier):
            with zipfile.ZipFile(self._root, mode='a', compression=zipfile.ZIP_DEFLATED) as myzip:
                path = self._path(identifier)
                myzip.writestr(path, data)
        else:
            if overwrite:
                self._update(self._path(identifier), data)
            else:
                raise FileExistsError(identifier)

    def get(self, identifier: str) -> str:
        path = self._path(identifier)
        try:
            with zipfile.ZipFile(self._root) as myzip:
                with myzip.open(path) as file:
                    return file.read().decode()
        except FileNotFoundError as fnf:
            raise KeyError(identifier) from fnf

    def exists(self, identifier: str) -> bool:
        path = self._path(identifier)
        with zipfile.ZipFile(self._root, 'r') as myzip:
            return path in myzip.namelist()

    def delete(self, identifier: str) -> None:
        if not self.exists(identifier):
            raise KeyError(identifier)
        self._update(self._path(identifier), None)

    def _update(self, filename: str, data: Optional[str]) -> None:
        # generate a temp file
        tmpfd, tmpname = tempfile.mkstemp(dir=os.path.dirname(self._root))
        os.close(tmpfd)

        # create a temp copy of the archive without filename            
        with zipfile.ZipFile(self._root, 'r') as zin:
            with zipfile.ZipFile(tmpname, 'w') as zout:
                zout.comment = zin.comment # preserve the comment
                for item in zin.infolist():
                    if item.filename != filename:
                        zout.writestr(item, zin.read(item.filename))

        # replace with the temp archive
        os.remove(self._root)
        os.rename(tmpname, self._root)

        # now add filename with its new data
        if data is not None:
            with zipfile.ZipFile(self._root, mode='a', compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr(filename, data)


class CachingBackend(StorageBackend):
    """Adds naive memory caching functionality to another StorageBackend.

    CachingBackend relies on another StorageBackend to provide real data IO functionality which
    it extends by caching already opened files in memory for faster subsequent access.

    Note that it does not flush the cache at any time and may thus not be suitable for long-time
    usage as it may consume increasing amounts of memory.
    """

    def __init__(self, backend: StorageBackend) -> None:
        """Create a new CachingBackend.

        Args:
            backend (StorageBackend): A StorageBackend that provides data
                IO functionality.
        """
        self._backend = backend
        self._cache = {}

    def put(self, identifier: str, data: str, overwrite: bool=False) -> None:
        if identifier in self._cache and not overwrite:
            raise FileExistsError(identifier)
        self._backend.put(identifier, data, overwrite)
        self._cache[identifier] = data

    def get(self, identifier: str) -> str:
        if identifier not in self._cache:
            self._cache[identifier] = self._backend.get(identifier)
        return self._cache[identifier]

    def exists(self, identifier: str) -> bool:
        return self._backend.exists(identifier)

    def delete(self, identifier: str) -> None:
        self._backend.delete(identifier)
        if identifier in self._cache:
            del self._cache[identifier]


class DictBackend(StorageBackend):
    """DictBackend uses a dictionary to store the data for convenience serialization."""
    def __init__(self) -> None:
        self._cache = {}

    def put(self, identifier: str, data: str, overwrite: bool=False) -> None:
        if identifier in self._cache and not overwrite:
            raise FileExistsError(identifier)
        self._cache[identifier] = data

    def get(self, identifier: str) -> str:
        return self._cache[identifier]

    def exists(self, identifier: str) -> bool:
        return identifier in self._cache
    
    @property
    def storage(self) -> Dict[str, str]:
        return self._cache

    def delete(self, identifier: str) -> None:
        del self._cache[identifier]


def get_type_identifier(obj: Any) -> str:
    """Return a unique type identifier for any object.

    Args:
        obj: The object for which to obtain a type identifier.
    Returns:
        The type identifier as a string.
    """
    return "{}.{}".format(obj.__module__, obj.__class__.__name__)


class SerializableMeta(DocStringABCMeta):
    deserialization_callbacks = dict()

    def __new__(mcs, name, bases, dct):
        cls = super().__new__(mcs, name, bases, dct)

        type_identifier = getattr(cls, 'get_type_identifier')()

        try:
            deserialization_function = getattr(cls, 'deserialize')
        except AttributeError:
            deserialization_function = cls
        mcs.deserialization_callbacks[type_identifier] = deserialization_function

        return cls


default_pulse_registry = weakref.WeakValueDictionary()


def get_default_pulse_registry() -> Union[weakref.WeakKeyDictionary, 'PulseStorage']:
    return default_pulse_registry


class Serializable(metaclass=SerializableMeta):
    """Any object that can be converted into a serialized representation for storage and back.

    Serializable is the interface used by Serializer to obtain representations of objects that
    need to be stored. It essentially provides the methods get_serialization_data, which returns
    a dictionary which contains all relevant properties of the Serializable object encoded as
    basic Python types, and deserialize, which is able to reconstruct the object from given
    such a dictionary.

    Additionally, a Serializable object MAY have a unique identifier, which indicates towards
    the Serializer that this object should be stored as a separate data item and accessed by
    reference instead of possibly embedding it into a containing Serializable's representation.

    See also:
        Serializer
    """

    type_identifier_name = '#type'
    identifier_name = '#identifier'

    def __init__(self, identifier: Optional[str]=None) -> None:
        """Initialize a Serializable.

        Args:
            identifier: An optional, non-empty identifier for this Serializable.
                If set, this Serializable will always be stored as a separate data item and never
                be embedded.
        Raises:
            ValueError: If identifier is the empty string
        """
        super().__init__()

        if identifier == '':
            raise ValueError("Identifier must not be empty.")
        self.__identifier = identifier

    def _register(self, registry: Optional[MutableMapping]=None) -> None:
        """Registers the Serializable in the global registry.

        This method MUST be called by subclasses at some point during init.
        Args:
            registry: An optional dict where the Serializable is registered. If None, it gets registered in the
                default_pulse_registry.
        Raises:
            RuntimeError: If a Serializable with the same name is already registered.
        """
        if registry is None:
            registry = default_pulse_registry

        if self.identifier and registry is not None:
            if self.identifier in registry:
                # trigger garbage collection in case the registered object isn't referenced anymore
                gc.collect(2)

                if self.identifier in registry:
                    raise RuntimeError('Pulse with name already exists', self.identifier)

            registry[self.identifier] = self

    @property
    def identifier(self) -> Optional[str]:
        """The (optional) identifier of this Serializable. Either a non-empty string or None."""
        return self.__identifier

    def get_serialization_data(self, serializer: Optional['Serializer']=None) -> Dict[str, Any]:
        """Return all data relevant for serialization as a dictionary containing only base types.

        Implementation hint:
        In the old serialization routines, if the Serializer contains complex objects which are itself
        Serializables, a serialized representation for these MUST be obtained by calling the dictify()
        method of serializer. The reason is that serializer may decide to either return a dictionary
        to embed or only a reference to the Serializable subelement. This is DEPRECATED behavior as of May 2018.
        In the new routines, this will happen automatically and every Serializable is only responsible for
        returning it's own data and leave nested Serializables in object form.

        For the transition time where both implementations are
        available, implementations of this method should support the old and new routines, using
        the presence of the serializer argument to differentiate between both. Don't make use of
        the implementation in this base class when implementing this method for the old routines.

        Args:
            serializer (Serializer): DEPRECATED (May 2018).A Serializer instance used to serialize
                complex subelements of this Serializable.
        Returns:
            A dictionary of Python base types (strings, integers, lists/tuples containing these,
                etc..) which fully represent the relevant properties of this Serializable for
                storing and later reconstruction as a Python object.
        """
        if serializer:
            warnings.warn("{c}.get_serialization_data(*) was called with a serializer argument, indicating deprecated behavior. Please switch to the new serialization routines.".format(c=self.__class__.__name__), DeprecationWarning)

        if self.identifier:
            return {self.type_identifier_name: self.get_type_identifier(), self.identifier_name: self.identifier}
        else:
            return {self.type_identifier_name: self.get_type_identifier()}

    @classmethod
    def get_type_identifier(cls) -> str:
        return "{}.{}".format(cls.__module__, cls.__name__)

    @classmethod
    def deserialize(cls, serializer: Optional['Serializer']=None, **kwargs) -> 'Serializable':
        """Reconstruct the Serializable object from a dictionary.

        Implementation hint:
            For greater clarity, implementations of this method should be precise in their return value,
            i.e., give their exact class name, and also replace the **kwargs argument by a list of
            arguments required, i.e., those returned by get_serialization_data.
            If this Serializable contains complex objects which are itself of type Serializable, their
            dictionary representations MUST be converted into objects using serializers deserialize()
            method when using the old serialization routines. This is DEPRECATED behavior.
            Using the new routines a serializable is only responsible to decode it's own dictionary,
            not those of nested objects (i.e., all incoming arguments are already processed by the
            serialization routines). For the transition time where both implementations are
            available, implementations of this method should support the old and new routines, using
            the presence of the serializer argument to differentiate between both. For the new routines,
            just call this base class function.
            After the transition period, subclasses likely need not implement deserialize separately anymore at all.

         Args:
             serializer: DEPRECATED (May 2018). A serializer instance used when deserializing subelements.
             <property_name>: All relevant properties of the object as keyword arguments. For every
                (key,value) pair returned by get_serialization_data, the same pair is given as
                keyword argument as input to this method.
         """
        if serializer:
            warnings.warn("{c}.deserialize(*) was called with a serializer argument, indicating deprecated behavior. Please switch to the new serialization routines.".format(c=cls.__name__), DeprecationWarning)

        return cls(**kwargs)


class AnonymousSerializable:
    """Any object that can be converted into a serialized representation for storage and back which NEVER has an
    identifier. This class is used for implicit serialization and does not work necessarily with dicts.

    The type information is not saved explicitly but implicitly by the position in the JSON-document.

    See also:
        Serializable

    # todo (lumip, 2018-05-30): this does not really have a purpose, especially in the new serialization ecosystem.. we should deprecate and remove it
    """

    def get_serialization_data(self) -> Any:
        """Return all data relevant for serialization as a JSON compatible type that is accepted as constructor argument

        Returns:
            A JSON compatible type that can be used to construct an equal object.
        """
        raise NotImplementedError()


class Serializer(object):
    """Serializes Serializable objects and stores them persistently.

    DEPRECATED as of May 2018. Serializer will be superseeded by the new serialization routines and
    PulseStorage class.

    Serializer provides methods to enable the conversion of Serializable objects (including nested
    Serializables) into (nested) dictionaries and serialized JSON-encodings of these and vice-versa.
    Additionally, it can also store these representations persistently using a StorageBackend
    instance.

    See also:
        Serializable
    """

    __FileEntry = NamedTuple("FileEntry", [('serialization', str), ('serializable', Serializable)])

    def __init__(self, storage_backend: StorageBackend) -> None:
        """Create a Serializer.

        Args:
            storage_backend (StorageBackend): The StorageBackend all objects will be stored in.
        """
        self.__subpulses = dict() # type: Dict[str, Serializer.__FileEntry]
        self.__storage_backend = storage_backend

        warnings.warn("Serializer is deprecated. Please switch to the new serialization routines.", DeprecationWarning)

    def dictify(self, serializable: Serializable) -> Union[str, Dict[str, Any]]:
        """Convert a Serializable into a dictionary representation.

        The Serializable is converted by calling its get_serialization_data() method. If it contains
        nested Serializables, these are also converted into dictionarys (or references), yielding
        a single dictionary representation of the outermost Serializable where all nested
        Serializables are either completely embedded or referenced by identifier.

        Args:
            serializable (Serializabe): The Serializable object to convert.
        Returns:
            A serialization dictionary, i.e., a dictionary of Python base types (strings, integers,
                lists/tuples containing these, etc..) which fully represent the relevant properties
                of the given Serializable for storing and later reconstruction as a Python object.
                Nested Serializables are either embedded or referenced by identifier.
        Raises:
            Exception if an identifier is assigned twice to different Serializable objects
                encountered by this Serializer during the conversion.
        See also:
            Serializable.get_serialization_data
        """
        repr_ = serializable.get_serialization_data(serializer=self)
        repr_['type'] = self.get_type_identifier(serializable)
        identifier = serializable.identifier
        if identifier is None:
            return repr_
        else:
            if identifier in self.__subpulses:
                if self.__subpulses[identifier].serializable is not serializable:
                    raise Exception("Identifier '{}' assigned twice.".format(identifier))
            else:
                self.__subpulses[identifier] = Serializer.__FileEntry(repr_, serializable)
            return identifier

    def __collect_dictionaries(self, serializable: Serializable) -> Dict[str, Dict[str, Any]]:
        """Convert a Serializable into a collection of dictionary representations.

        The Serializable is converted by calling its get_serialization_data() method. If it contains
        nested Serializables, these are also converted into dictionarys (or references), yielding
        a dictionary representation of the outermost Serializable where all nested
        Serializables are either completely embedded or referenced by identifier as it is returned
        by dictify. If nested Serializables shall be stored separately, their dictionary
        representations are collected. Collection_dictionaries returns a dictionary of all
        serialization dictionaries where the keys are the identifiers of the Serializables.

        Args:
            serializable (Serializabe): The Serializable object to convert.
        Returns:
            A dictionary containing serialization dictionary for each separately stored Serializable
                nested in the given Serializable.
        See also:
            dictify
        """
        self.__subpulses = dict()
        repr_ = self.dictify(serializable)
        filedict = dict()
        for identifier in self.__subpulses:
            filedict[identifier] = self.__subpulses[identifier].serialization
        if isinstance(repr_, dict):
            filedict[''] = repr_
        return filedict

    @staticmethod
    def get_type_identifier(obj: Any) -> str:
        """Return a unique type identifier for any object.

        Args:
            obj: The object for which to obtain a type identifier.
        Returns:
            The type identifier as a string.
        """
        return "{}.{}".format(obj.__module__, obj.__class__.__name__)

    def serialize(self, serializable: Serializable, overwrite=False) -> None:
        """Serialize and store a Serializable.

        The given Serializable and all nested Serializables that are to be stored separately will be
        converted into a serial string representation by obtaining their dictionary representation,
        encoding them as a JSON-string and storing them in the StorageBackend.

        If no identifier is given for the Serializable, "main" will be used.

        If an identifier is already in use in the StorageBackend, associated data will be replaced.

        Args:
            serializable (Serializable): The Serializable to serialize and store
        """
        warnings.warn("Serializer is deprecated. Please switch to the new serialization routines.", DeprecationWarning)
        repr_ = self.__collect_dictionaries(serializable)
        for identifier in repr_:
            storage_identifier = identifier
            if identifier == '':
                storage_identifier = 'main'
            json_str = json.dumps(repr_[identifier], indent=4, sort_keys=True, cls=ExtendedJSONEncoder)
            self.__storage_backend.put(storage_identifier, json_str, overwrite)

    def deserialize(self, representation: Union[str, Dict[str, Any]]) -> Serializable:
        """Load a stored Serializable object or convert dictionary representation back to the
            corresponding Serializable.

        Args:
            representation: A serialization dictionary representing a Serializable object or the
                identifier of a Serializable object to load from the StorageBackend.
        Returns:
            The Serializable object instantiated from its serialized representation.
        See also:
            Serializable.deserialize
        """
        warnings.warn("Serializer is deprecated. Please switch to the new serialization routines.", DeprecationWarning)
        if isinstance(representation, str):
            if representation in self.__subpulses:
                return self.__subpulses[representation].serializable
        
        if isinstance(representation, str):
            repr_ = json.loads(self.__storage_backend.get(representation))
            repr_['identifier'] = representation
        else:
            repr_ = dict(representation)

        module_name, class_name = repr_['type'].rsplit('.', 1)
        module = __import__(module_name, fromlist=[class_name])
        class_ = getattr(module, class_name)
        
        repr_to_store = repr_.copy()
        repr_.pop('type')
        
        serializable = class_.deserialize(self, **repr_)
        
        if 'identifier' in repr_:
            identifier = repr_['identifier']
            self.__subpulses[identifier] = self.__FileEntry(repr_, serializable)
        return serializable


class PulseStorage:
    StorageEntry = NamedTuple('StorageEntry', [('serialization', str), ('serializable', Serializable)])

    def __init__(self,
                 storage_backend: StorageBackend) -> None:
        self._storage_backend = storage_backend

        self._temporary_storage = dict() # type: Dict[str, StorageEntry]
        self._transaction_storage = None

    def _deserialize(self, serialization: str) -> Serializable:
        decoder = JSONSerializableDecoder(storage=self)
        serializable = decoder.decode(serialization)
        return serializable

    def _load_and_deserialize(self, identifier: str) -> StorageEntry:
        serialization = self._storage_backend[identifier]
        serializable = self._deserialize(serialization)
        self._temporary_storage[identifier] = PulseStorage.StorageEntry(serialization=serialization,
                                                                        serializable=serializable)
        return self._temporary_storage[identifier]

    @property
    def temporary_storage(self) -> Dict[str, StorageEntry]:
        return self._temporary_storage

    def __contains__(self, identifier) -> bool:
        return identifier in self._temporary_storage or identifier in self._storage_backend

    def __getitem__(self, identifier: str) -> Serializable:
        if identifier not in self._temporary_storage:
            self._load_and_deserialize(identifier)
        return self._temporary_storage[identifier].serializable

    def __setitem__(self, identifier: str, serializable: Serializable) -> None:
        if identifier in self._temporary_storage:
            if self.temporary_storage[identifier].serializable is serializable:
                return
            else:
                raise RuntimeError('Identifier assigned twice with different objects', identifier)
        elif identifier in self._storage_backend:
            raise RuntimeError('Identifier already assigned in storage backend', identifier)
        self.overwrite(identifier, serializable)

    def __delitem__(self, identifier: str) -> None:
        """Delete an item from temporary storage and storage backend.

        Does not raise an error if the deleted pulse is only in the storage backend. Assumes that all pulses
        contained in temporary storage are always also contained in the storage backend.
        """
        del self._storage_backend[identifier]
        try:
            del self._temporary_storage[identifier]
        except KeyError:
            pass

    def overwrite(self, identifier: str, serializable: Serializable) -> None:
        """Use this method actively change a pulse"""

        is_transaction_begin = (self._transaction_storage is None)
        try:
            if is_transaction_begin:
                self._transaction_storage = dict()

            encoder = JSONSerializableEncoder(self, sort_keys=True, indent=4)

            serialization_data = serializable.get_serialization_data()
            serialized = encoder.encode(serialization_data)
            self._transaction_storage[identifier] = self.StorageEntry(serialized, serializable)

            if is_transaction_begin:
                for identifier, entry in self._transaction_storage.items():
                    self._storage_backend.put(identifier, entry.serialization, overwrite=True)
                self._temporary_storage.update(**self._transaction_storage)

        finally:
            if is_transaction_begin:
                self._transaction_storage = None

    def clear(self) -> None:
        self._temporary_storage.clear()

    @contextmanager
    def as_default_registry(self) -> Any:
        global default_pulse_registry
        previous_registry = default_pulse_registry
        default_pulse_registry = self
        try:
            yield self
        finally:
            default_pulse_registry = previous_registry

    def set_to_default_registry(self) -> None:
        global default_pulse_registry
        default_pulse_registry = self


class JSONSerializableDecoder(json.JSONDecoder):

    def __init__(self, storage: Mapping, *args, **kwargs) -> None:
        super().__init__(*args, object_hook=self.filter_serializables, **kwargs)

        self.storage = storage

    def filter_serializables(self, obj_dict) -> Any:
        if Serializable.type_identifier_name in obj_dict:
            type_identifier = obj_dict.pop(Serializable.type_identifier_name)

            if Serializable.identifier_name in obj_dict:
                obj_identifier = obj_dict.pop(Serializable.identifier_name)
            else:
                obj_identifier = None

            if type_identifier == 'reference':
                if not obj_identifier:
                    raise RuntimeError('Reference without identifier')
                return self.storage[obj_identifier]

            else:
                deserialization_callback = SerializableMeta.deserialization_callbacks[type_identifier]

                # if the storage is the default registry, we would get conflicts when the Serializable tries to register
                # itself on construction. Pass an empty dict as registry keyword argument in this case.
                # calling PulseStorage objects will take care of registering.
                # (solution to issue #301: https://github.com/qutech/qc-toolkit/issues/301 )
                registry = None
                if get_default_pulse_registry() is self.storage:
                    registry = dict()

                return deserialization_callback(identifier=obj_identifier, registry=registry, **obj_dict)
        return obj_dict


class JSONSerializableEncoder(json.JSONEncoder):
    """"""

    def __init__(self, storage: MutableMapping, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        self.storage = storage

    def default(self, o: Any) -> Any:
        if isinstance(o, Serializable):
            if o.identifier:
                if o.identifier not in self.storage:
                    self.storage[o.identifier] = o
                elif o is not self.storage[o.identifier]:
                    raise RuntimeError('Trying to store a subpulse with an identifier that is already taken.')


                return {Serializable.type_identifier_name: 'reference',
                        Serializable.identifier_name: o.identifier}
            else:
                return o.get_serialization_data()

        elif isinstance(o, AnonymousSerializable):
            return o.get_serialization_data()

        elif type(o) is set:
            return list(o)

        else:
            return super().default(o)


class ExtendedJSONEncoder(json.JSONEncoder):
    """Encodes AnonymousSerializable and sets as lists.

    Deprecated as of May 2018. To be replaced by JSONSerializableEncoder."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def default(self, o: Any) -> Any:
        if isinstance(o, AnonymousSerializable):
            return o.get_serialization_data()
        elif type(o) is set:
            return list(o)
        else:
            return super().default(o)
