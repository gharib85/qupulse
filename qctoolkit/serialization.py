""" This module provides serialization and storage functionality.

Classes:
    StorageBackend: Abstract representation of a data storage
    FilesystemBackend: Implementation of a file system data storage
    CachingBackend: A caching decorator for StorageBackends
    Serializable: An interface for serializable objects
    Serializer: Converts Serializables to a serial representation as a string and vice-versa
"""

from abc import ABCMeta, abstractmethod, abstractstaticmethod
from typing import Dict, Any, Optional, NamedTuple, Union
import os.path
import json

__all__ = ["StorageBackend", "FilesystemBackend", "CachingBackend", "Serializable", "Serializer"]


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
            DataExistsException if overwrite is False and there already exists data which
                is associated with the given identifier.
        """

    @abstractmethod
    def get(self, identifier: str) -> str:
        """Retrieve the data string with the given identifier.

        Args:
            identifier (str): The identifier of the data to be retrieved.
        Returns:
            A serialized string of the data associated with the given identifier, if present.
        Raises:
            DataMissingException if no data is associated with the given identifier.
        """

    @abstractmethod
    def exists(self, identifier: str) -> bool:
        """Check if data is stored for the given identifier.

        Args:
            identifier (str): The identifier for which presence of data shall be checked.
        Returns:
            True, if stored data is associated with the given identifier.
        """


class FilesystemBackend(StorageBackend):
    """A StorageBackend implementation based on a regular filesystem.

    Data will be stored in plain text files in a directory. The directory is given in the
    constructor of this FilesystemBackend. For each data item, a separate file is created an named
    after the corresponding identifier.
    """

    def __init__(self, root: str='.') -> None:
        """Create a new FilesystemBackend.

        Args:
            root (str): The path of the directory in which all data files are located. (default: ".",
                i.e. the current directory)
        Raises:
            NotADirectoryError if root is not a valid directory path.
        """
        if not os.path.isdir(root):
            raise NotADirectoryError()
        self.__root = os.path.abspath(root)

    def put(self, identifier: str, data: str, overwrite: bool=False) -> None:
        path = os.path.join(self.__root, identifier)
        if self.exists(identifier) and not overwrite:
            raise FileExistsError(identifier)
        with open(path, 'w') as file:
            file.write(data)

    def get(self, identifier: str) -> str:
        path = os.path.join(self.__root, identifier)
        try:
            with open(path) as file:
                return file.read()
        except FileNotFoundError as fnf:
            raise FileNotFoundError(identifier) from fnf

    def exists(self, identifier: str) -> bool:
        path = os.path.join(self.__root, identifier)
        return os.path.isfile(path)


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
        self.__backend = backend
        self.__cache = {}

    def put(self, identifier: str, data: str, overwrite: bool=False) -> None:
        if identifier in self.__cache and not overwrite:
            raise FileExistsError(identifier)
        self.__backend.put(identifier, data, overwrite)
        self.__cache[identifier] = data

    def get(self, identifier: str) -> str:
        if identifier not in self.__cache:
            self.__cache[identifier] = self.__backend.get(identifier)
        return self.__cache[identifier]

    def exists(self, identifier: str) -> bool:
        return self.__backend.exists(identifier)


class Serializable(metaclass=ABCMeta):
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

    def __init__(self, identifier: Optional[str]=None) -> None:
        """Initialize a Serializable.

        Args:
            identifier (str): An optional, non-empty identifier for this Serializable.
                If set, this Serializable will always be stored as a separate data item and never
                be embedded. (default=None)
        Raises:
            ValueError if identifier is the empty string
        """
        super().__init__()
        if identifier == '':
            raise ValueError("Identifier must not be empty.")
        self.__identifier = identifier

    @property
    def identifier(self) -> Optional[str]:
        """The (optional) identifier of this Serializable. Either a non-empty string or None."""
        return self.__identifier

    @abstractmethod
    def get_serialization_data(self, serializer: 'Serializer') -> Dict[str, Any]:
        """Return all data relevant for serialization as a dictionary containing only base types.

        Implementation hint:
        If the Serializer contains complex objects which are itself Serializables, a serialized
        representation for these MUST be obtained by calling the dictify() method of
        serializer. The reason is that serializer may decide to either return a dictionary to embed
        or only a reference to the Serializable subelement.

        Args:
            serializer (Serializer): A Serializer instance used to serialize complex subelements of
                this Serializable.
        Returns:
            A dictionary of Python base types (strings, integers, lists/tuples containing these,
                etc..) which fully represent the relevant properties of this Serializable for
                storing and later reconstruction as a Python object.
        """

    @abstractstaticmethod
    def deserialize(serializer: 'Serializer', **kwargs) -> 'Serializable':
        """Reconstruct the Serializable object from a dictionary.

        Implementation hint:
        For greater clarity, implementations of this method should be precise in their return value,
        i.e., give their exact class name, and also replace the **kwargs argument by a list of
        arguments required, i.e., those returned by get_serialization_data.
        If this Serializable contains complex objects which are itself Serializables, their
        dictionary representations MUST be converted into objects using serializers deserialize()
        method.

         Args:
             serializer (Serializer): A serializer instance used when deserializing subelements.
             <property_name>: All relevant properties of the object as keyword arguments. For every
                (key,value) pair returned by get_serialization_data, the same pair is given as
                keyword argument as input to this method.
         """


class Serializer(object):
    """Serializes Serializable objects and stores them persistently.

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
        repr_ = serializable.get_serialization_data(self)
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

    def serialize(self, serializable: Serializable) -> None:
        """Serialize and store a Serializable.

        The given Serializable and all nested Serializables that are to be stored separately will be
        converted into a serial string representation by obtaining their dictionary representation,
        encoding them as a JSON-string and storing them in the StorageBackend.

        If no identifier is given for the Serializable, "main" will be used.

        If an identifier is already in use in the StorageBackend, associated data will be replaced.

        Args:
            serializable (Serializable): The Serializable to serialize and store
        """
        repr_ = self.__collect_dictionaries(serializable)
        for identifier in repr_:
            storage_identifier = identifier
            if identifier == '':
                storage_identifier = 'main'
            json_str = json.dumps(repr_[identifier], indent=4, sort_keys=True)
            self.__storage_backend.put(storage_identifier, json_str, True)

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
        if isinstance(representation, str):
            repr_ = json.loads(self.__storage_backend.get(representation))
            repr_['identifier'] = representation
        else:
            repr_ = dict(representation)

        module_name, class_name = repr_['type'].rsplit('.', 1)
        module = __import__(module_name, fromlist=[class_name])
        class_ = getattr(module, class_name)

        repr_.pop('type')
        return class_.deserialize(self, **repr_)
