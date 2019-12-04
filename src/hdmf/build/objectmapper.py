import re
import numpy as np
import warnings
from collections import OrderedDict
from copy import copy
from datetime import datetime

from ..utils import docval, getargs, ExtenderMeta, get_docval
from ..container import AbstractContainer, Container, Data, DataRegion
from ..spec import Spec, AttributeSpec, CoordSpec, DatasetSpec, GroupSpec, LinkSpec, NAME_WILDCARD, RefSpec
from ..data_utils import DataIO, AbstractDataChunkIterator
from ..query import ReferenceResolver
from ..spec.spec import BaseStorageSpec
from .builders import DatasetBuilder, GroupBuilder, LinkBuilder, Builder, ReferenceBuilder, RegionBuilder
from .map import Proxy, BuildManager
from .warnings import OrphanContainerWarning, MissingRequiredWarning


_const_arg = '__constructor_arg'


@docval({'name': 'name', 'type': str, 'doc': 'the name of the constructor argument'},
        is_method=False)
def _constructor_arg(**kwargs):
    '''Decorator to override the default mapping scheme for a given constructor argument.

    Decorate ObjectMapper methods with this function when extending ObjectMapper to override the default
    scheme for mapping between AbstractContainer and Builder objects. The decorated method should accept as its
    first argument the Builder object that is being mapped. The method should return the value to be passed
    to the target AbstractContainer class constructor argument given by *name*.
    '''
    name = getargs('name', kwargs)

    def _dec(func):
        setattr(func, _const_arg, name)
        return func
    return _dec


_obj_attr = '__object_attr'


@docval({'name': 'name', 'type': str, 'doc': 'the name of the constructor argument'},
        is_method=False)
def _object_attr(**kwargs):
    '''Decorator to override the default mapping scheme for a given object attribute.

    Decorate ObjectMapper methods with this function when extending ObjectMapper to override the default
    scheme for mapping between AbstractContainer and Builder objects. The decorated method should accept as its
    first argument the AbstractContainer object that is being mapped. The method should return the child Builder
    object (or scalar if the object attribute corresponds to an AttributeSpec) that represents the
    attribute given by *name*.
    '''
    name = getargs('name', kwargs)

    def _dec(func):
        setattr(func, _obj_attr, name)
        return func
    return _dec


def _unicode(s):
    """
    A helper function for converting to Unicode
    """
    if isinstance(s, str):
        return s
    elif isinstance(s, bytes):
        return s.decode('utf-8')
    else:
        raise ValueError("Expected unicode or ascii string, got %s" % type(s))


def _ascii(s):
    """
    A helper function for converting to ASCII
    """
    if isinstance(s, str):
        return s.encode('ascii', 'backslashreplace')
    elif isinstance(s, bytes):
        return s
    else:
        raise ValueError("Expected unicode or ascii string, got %s" % type(s))


class ObjectMapper(metaclass=ExtenderMeta):
    '''A class for mapping between Spec objects and AbstractContainer attributes

    '''

    __dtypes = {
        "float": np.float32,
        "float32": np.float32,
        "double": np.float64,
        "float64": np.float64,
        "long": np.int64,
        "int64": np.int64,
        "uint64": np.uint64,
        "int": np.int32,
        "int32": np.int32,
        "int16": np.int16,
        "int8": np.int8,
        "bool": np.bool_,
        "text": _unicode,
        "text": _unicode,
        "utf": _unicode,
        "utf8": _unicode,
        "utf-8": _unicode,
        "ascii": _ascii,
        "str": _ascii,
        "isodatetime": _ascii,
        "uint32": np.uint32,
        "uint16": np.uint16,
        "uint8": np.uint8,
        "uint": np.uint32
    }

    __no_convert = set()

    @classmethod
    def __resolve_dtype(cls, given, specified):
        """
        Determine the dtype to use from the dtype of the given value and the specified dtype.
        This amounts to determining the greater precision of the two arguments, but also
        checks to make sure the same base dtype is being used.
        """
        g = np.dtype(given)
        s = np.dtype(specified)
        if g.itemsize <= s.itemsize:
            return s.type
        else:
            if g.name[:3] != s.name[:3]:    # different types
                if s.itemsize < 8:
                    msg = "expected %s, received %s - must supply %s or higher precision" % (s.name, g.name, s.name)
                else:
                    msg = "expected %s, received %s - must supply %s" % (s.name, g.name, s.name)
                raise ValueError(msg)
            else:
                return g.type

    @classmethod
    def no_convert(cls, obj_type):
        """
        Specify an object type that ObjectMappers should not convert.
        """
        cls.__no_convert.add(obj_type)

    @classmethod
    def convert_dtype(cls, spec, value):
        """
        Convert values to the specified dtype. For example, if a literal int
        is passed in to a field that is specified as a unsigned integer, this function
        will convert the Python int to a numpy unsigned int.

        :return: The function returns a tuple consisting of 1) the value, and 2) the data type.
                 The value is returned as the function may convert the input value to comply
                 with the dtype specified in the schema.
        """
        ret, ret_dtype = cls.__check_edgecases(spec, value)
        if ret is not None or ret_dtype is not None:
            return ret, ret_dtype
        spec_dtype = cls.__dtypes[spec.dtype]
        if isinstance(value, np.ndarray):
            if spec_dtype is _unicode:
                ret = value.astype('U')
                ret_dtype = "utf8"
            elif spec_dtype is _ascii:
                ret = value.astype('S')
                ret_dtype = "ascii"
            else:
                dtype_func = cls.__resolve_dtype(value.dtype, spec_dtype)
                ret = np.asarray(value).astype(dtype_func)
                ret_dtype = ret.dtype.type
        elif isinstance(value, (tuple, list)):
            if len(value) == 0:
                return value, spec_dtype
            ret = list()
            for elem in value:
                tmp, tmp_dtype = cls.convert_dtype(spec, elem)
                ret.append(tmp)
            ret = type(value)(ret)
            ret_dtype = tmp_dtype
        elif isinstance(value, AbstractDataChunkIterator):
            ret = value
            ret_dtype = cls.__resolve_dtype(value.dtype, spec_dtype)
        else:
            if spec_dtype in (_unicode, _ascii):
                ret_dtype = 'ascii'
                if spec_dtype == _unicode:
                    ret_dtype = 'utf8'
                ret = spec_dtype(value)
            else:
                dtype_func = cls.__resolve_dtype(type(value), spec_dtype)
                ret = dtype_func(value)
                ret_dtype = type(ret)
        return ret, ret_dtype

    @classmethod
    def __check_edgecases(cls, spec, value):
        """
        Check edge cases in converting data to a dtype
        """
        if value is None:
            dt = spec.dtype
            if isinstance(dt, RefSpec):
                dt = dt.reftype
            return None, dt
        if isinstance(spec.dtype, list):
            # compound dtype - Since the I/O layer needs to determine how to handle these,
            # return the list of DtypeSpecs
            return value, spec.dtype
        if isinstance(value, DataIO):
            return value, cls.convert_dtype(spec, value.data)[1]
        if spec.dtype is None or spec.dtype == 'numeric' or type(value) in cls.__no_convert:
            # infer type from value
            if hasattr(value, 'dtype'):  # covers numpy types, AbstractDataChunkIterator
                return value, value.dtype.type
            if isinstance(value, (list, tuple)):
                if len(value) == 0:
                    msg = "cannot infer dtype of empty list or tuple. Please use numpy array with specified dtype."
                    raise ValueError(msg)
                return value, cls.__check_edgecases(spec, value[0])[1]  # infer dtype from first element
            ret_dtype = type(value)
            if ret_dtype is str:
                ret_dtype = 'utf8'
            elif ret_dtype is bytes:
                ret_dtype = 'ascii'
            return value, ret_dtype
        if isinstance(spec.dtype, RefSpec):
            if not isinstance(value, ReferenceBuilder):
                msg = "got RefSpec for value of type %s" % type(value)
                raise ValueError(msg)
            return value, spec.dtype
        if spec.dtype is not None and spec.dtype not in cls.__dtypes:
            msg = "unrecognized dtype: %s -- cannot convert value" % spec.dtype
            raise ValueError(msg)
        return None, None

    _const_arg = '__constructor_arg'

    @staticmethod
    @docval({'name': 'name', 'type': str, 'doc': 'the name of the constructor argument'},
            is_method=False)
    def constructor_arg(**kwargs):
        '''Decorator to override the default mapping scheme for a given constructor argument.

        Decorate ObjectMapper methods with this function when extending ObjectMapper to override the default
        scheme for mapping between AbstractContainer and Builder objects. The decorated method should accept as its
        first argument the Builder object that is being mapped. The method should return the value to be passed
        to the target AbstractContainer class constructor argument given by *name*.
        '''
        name = getargs('name', kwargs)
        return _constructor_arg(name)

    _obj_attr = '__object_attr'

    @staticmethod
    @docval({'name': 'name', 'type': str, 'doc': 'the name of the constructor argument'},
            is_method=False)
    def object_attr(**kwargs):
        '''Decorator to override the default mapping scheme for a given object attribute.

        Decorate ObjectMapper methods with this function when extending ObjectMapper to override the default
        scheme for mapping between AbstractContainer and Builder objects. The decorated method should accept as its
        first argument the AbstractContainer object that is being mapped. The method should return the child Builder
        object (or scalar if the object attribute corresponds to an AttributeSpec) that represents the
        attribute given by *name*.
        '''
        name = getargs('name', kwargs)
        return _object_attr(name)

    @staticmethod
    def __is_attr(attr_val):
        return hasattr(attr_val, _obj_attr)

    @staticmethod
    def __get_obj_attr(attr_val):
        return getattr(attr_val, _obj_attr)

    @staticmethod
    def __is_constructor_arg(attr_val):
        return hasattr(attr_val, _const_arg)

    @staticmethod
    def __get_cargname(attr_val):
        return getattr(attr_val, _const_arg)

    @ExtenderMeta.post_init
    def __gather_procedures(cls, name, bases, classdict):
        if hasattr(cls, 'constructor_args'):
            cls.constructor_args = copy(cls.constructor_args)
        else:
            cls.constructor_args = dict()
        if hasattr(cls, 'obj_attrs'):
            cls.obj_attrs = copy(cls.obj_attrs)
        else:
            cls.obj_attrs = dict()
        for name, func in cls.__dict__.items():
            if cls.__is_constructor_arg(func):
                cls.constructor_args[cls.__get_cargname(func)] = getattr(cls, name)
            elif cls.__is_attr(func):
                cls.obj_attrs[cls.__get_obj_attr(func)] = getattr(cls, name)

    @docval({'name': 'spec', 'type': (DatasetSpec, GroupSpec),
             'doc': 'The specification for mapping objects to builders'})
    def __init__(self, **kwargs):
        """ Create a map from AbstractContainer attributes to specifications """
        spec = getargs('spec', kwargs)
        self.__spec = spec
        self.__data_type_key = spec.type_key()
        self.__spec2attr = dict()
        self.__attr2spec = dict()
        self.__spec2carg = dict()
        self.__carg2spec = dict()
        self.__map_spec(spec)

    @property
    def spec(self):
        ''' the Spec used in this ObjectMapper '''
        return self.__spec

    @_constructor_arg('name')
    def get_container_name(self, *args):
        builder = args[0]
        return builder.name

    @classmethod
    @docval({'name': 'spec', 'type': Spec, 'doc': 'the specification to get the name for'})
    def convert_dt_name(cls, **kwargs):
        '''Get the attribute name corresponding to a specification'''
        spec = getargs('spec', kwargs)
        if spec.data_type_def is not None:
            name = spec.data_type_def
        elif spec.data_type_inc is not None:
            name = spec.data_type_inc
        else:
            raise ValueError('found spec without name or data_type')
        s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
        name = re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()
        if name[-1] != 's' and spec.is_many():
            name += 's'
        return name

    @classmethod
    def __get_fields(cls, name_stack, all_names, spec):
        name = spec.name
        if spec.name is None:
            name = cls.convert_dt_name(spec)
        name_stack.append(name)
        name = '__'.join(name_stack)
        all_names[name] = spec
        if isinstance(spec, BaseStorageSpec):
            if not (spec.data_type_def is None and spec.data_type_inc is None):
                # don't get names for components in data_types
                name_stack.pop()
                return
            for subspec in spec.attributes:
                cls.__get_fields(name_stack, all_names, subspec)
            if isinstance(spec, GroupSpec):
                for subspec in spec.datasets:
                    cls.__get_fields(name_stack, all_names, subspec)
                for subspec in spec.groups:
                    cls.__get_fields(name_stack, all_names, subspec)
                for subspec in spec.links:
                    cls.__get_fields(name_stack, all_names, subspec)
        name_stack.pop()

    @classmethod
    @docval({'name': 'spec', 'type': Spec, 'doc': 'the specification to get the object attribute names for'})
    def get_attr_names(cls, **kwargs):
        '''Get the attribute names for each subspecification in a Spec'''
        spec = getargs('spec', kwargs)
        names = OrderedDict()
        for subspec in spec.attributes:
            cls.__get_fields(list(), names, subspec)
        if isinstance(spec, GroupSpec):
            for subspec in spec.groups:
                cls.__get_fields(list(), names, subspec)
            for subspec in spec.datasets:
                cls.__get_fields(list(), names, subspec)
            for subspec in spec.links:
                cls.__get_fields(list(), names, subspec)
        return names

    def __map_spec(self, spec):
        attr_names = self.get_attr_names(spec)
        for k, v in attr_names.items():
            self.map_spec(k, v)

    @docval({"name": "attr_name", "type": str, "doc": "the name of the object to map"},
            {"name": "spec", "type": Spec, "doc": "the spec to map the attribute to"})
    def map_attr(self, **kwargs):
        """ Map an attribute to spec. Use this to override default behavior """
        attr_name, spec = getargs('attr_name', 'spec', kwargs)
        self.__spec2attr[spec] = attr_name
        self.__attr2spec[attr_name] = spec

    @docval({"name": "attr_name", "type": str, "doc": "the name of the attribute"})
    def get_attr_spec(self, **kwargs):
        """ Return the Spec for a given attribute """
        attr_name = getargs('attr_name', kwargs)
        return self.__attr2spec.get(attr_name)

    @docval({"name": "carg_name", "type": str, "doc": "the name of the constructor argument"})
    def get_carg_spec(self, **kwargs):
        """ Return the Spec for a given constructor argument """
        carg_name = getargs('carg_name', kwargs)
        return self.__carg2spec.get(carg_name)

    @docval({"name": "const_arg", "type": str, "doc": "the name of the constructor argument to map"},
            {"name": "spec", "type": Spec, "doc": "the spec to map the attribute to"})
    def map_const_arg(self, **kwargs):
        """ Map an attribute to spec. Use this to override default behavior """
        const_arg, spec = getargs('const_arg', 'spec', kwargs)
        self.__spec2carg[spec] = const_arg
        self.__carg2spec[const_arg] = spec

    @docval({"name": "spec", "type": Spec, "doc": "the spec to map the attribute to"})
    def unmap(self, **kwargs):
        """ Removing any mapping for a specification. Use this to override default mapping """
        spec = getargs('spec', kwargs)
        self.__spec2attr.pop(spec, None)
        self.__spec2carg.pop(spec, None)

    @docval({"name": "attr_carg", "type": str, "doc": "the constructor argument/object attribute to map this spec to"},
            {"name": "spec", "type": Spec, "doc": "the spec to map the attribute to"})
    def map_spec(self, **kwargs):
        """ Map the given specification to the construct argument and object attribute """
        spec, attr_carg = getargs('spec', 'attr_carg', kwargs)
        self.map_const_arg(attr_carg, spec)
        self.map_attr(attr_carg, spec)

    def __get_override_carg(self, *args):
        name = args[0]
        remaining_args = tuple(args[1:])
        if name in self.constructor_args:
            func = self.constructor_args[name]
            return func(self, *remaining_args)
        return None

    def __get_override_attr(self, name, container, manager):
        if name in self.obj_attrs:
            func = self.obj_attrs[name]
            return func(self, container, manager)
        return None

    @docval({"name": "spec", "type": Spec, "doc": "the spec to get the attribute for"},
            returns='the attribute name', rtype=str)
    def get_attribute(self, **kwargs):
        ''' Get the object attribute name for the given Spec '''
        spec = getargs('spec', kwargs)
        val = self.__spec2attr.get(spec, None)
        return val

    @docval({"name": "spec", "type": Spec, "doc": "the spec to get the attribute value for"},
            {"name": "container", "type": AbstractContainer, "doc": "the container to get the attribute value from"},
            {"name": "manager", "type": BuildManager, "doc": "the BuildManager used for managing this build"},
            returns='the value of the attribute')
    def get_attr_value(self, **kwargs):
        ''' Get the value of the attribute corresponding to this spec from the given container '''
        spec, container, manager = getargs('spec', 'container', 'manager', kwargs)
        attr_name = self.get_attribute(spec)
        if attr_name is None:
            return None
        attr_val = self.__get_override_attr(attr_name, container, manager)
        if attr_val is None:
            try:
                attr_val = getattr(container, attr_name)
            except AttributeError:
                # raise error if an expected attribute (based on the spec) does not exist on a Container object
                msg = "Container '%s' (%s) does not have attribute '%s'" % (container.name, type(container), attr_name)
                raise Exception(msg)
            if attr_val is not None:
                attr_val = self.__convert_value(attr_val, spec)
            # else: attr_val is an attribute on the Container and its value is None
        return attr_val

    def __convert_value(self, value, spec):
        """
        Convert string types to the specified dtype
        """
        ret = value
        if isinstance(spec, AttributeSpec):
            if 'text' in spec.dtype:
                if spec.shape is not None or spec.dims is not None:
                    ret = list(map(str, value))
                else:
                    ret = str(value)
        elif isinstance(spec, DatasetSpec):
            # TODO: make sure we can handle specs with data_type_inc set
            if spec.data_type_inc is not None:
                ret = value
            else:
                if spec.dtype is not None:
                    string_type = None
                    if 'text' in spec.dtype:
                        string_type = str
                    elif 'ascii' in spec.dtype:
                        string_type = bytes
                    elif 'isodatetime' in spec.dtype:
                        string_type = datetime.isoformat
                    if string_type is not None:
                        if spec.shape is not None or spec.dims is not None:
                            ret = list(map(string_type, value))
                        else:
                            ret = string_type(value)
                        # copy over any I/O parameters if they were specified
                        if isinstance(value, DataIO):
                            params = value.get_io_params()
                            params['data'] = ret
                            ret = value.__class__(**params)
        return ret

    @docval({"name": "spec", "type": Spec, "doc": "the spec to get the constructor argument for"},
            returns="the name of the constructor argument", rtype=str)
    def get_const_arg(self, **kwargs):
        ''' Get the constructor argument for the given Spec '''
        spec = getargs('spec', kwargs)
        return self.__spec2carg.get(spec, None)

    @docval({"name": "container", "type": AbstractContainer, "doc": "the container to convert to a Builder"},
            {"name": "manager", "type": BuildManager, "doc": "the BuildManager to use for managing this build"},
            {"name": "parent", "type": Builder, "doc": "the parent of the resulting Builder", 'default': None},
            {"name": "source", "type": str,
             "doc": "the source of container being built i.e. file path", 'default': None},
            {"name": "builder", "type": GroupBuilder, "doc": "the Builder to build on", 'default': None},
            {"name": "spec_ext", "type": BaseStorageSpec, "doc": "a spec extension", 'default': None},
            returns="the Builder representing the given AbstractContainer", rtype=Builder)
    def build(self, **kwargs):
        ''' Convert a AbstractContainer to a Builder representation '''
        container, manager, parent, source = getargs('container', 'manager', 'parent', 'source', kwargs)
        spec_ext, builder = getargs('spec_ext', 'builder', kwargs)
        name = manager.get_builder_name(container)
        if isinstance(self.__spec, GroupSpec):
            if builder is None:
                builder = GroupBuilder(name, parent=parent, source=source)
            self.__add_datasets(builder, self.__spec.datasets, container, manager, source)
            self.__add_groups(builder, self.__spec.groups, container, manager, source)
            self.__add_links(builder, self.__spec.links, container, manager, source)
        else:
            if not isinstance(container, Data):
                raise ValueError("'container' must be of type Data with DatasetSpec")

            spec_dtype, spec_shape, spec = self.__check_dset_spec(self.spec, spec_ext)
            if isinstance(spec_dtype, RefSpec):
                # a dataset of references
                bldr_data = self.__get_ref_builder(spec_dtype, spec_shape, container, manager)
                builder = DatasetBuilder(name, bldr_data, parent=parent, source=source, dtype=spec_dtype.reftype)
            elif isinstance(spec_dtype, list):
                # a compound dataset
                # check for any references in the compound dtype, and convert them if necessary
                refs = [(i, subt) for i, subt in enumerate(spec_dtype) if isinstance(subt.dtype, RefSpec)]
                bldr_data = copy(container.data)
                bldr_data = list()
                for i, row in enumerate(container.data):
                    tmp = list(row)
                    for j, subt in refs:
                        tmp[j] = self.__get_ref_builder(subt.dtype, None, row[j], manager)
                    bldr_data.append(tuple(tmp))
                try:
                    bldr_data, dtype = self.convert_dtype(spec, bldr_data)
                except Exception as ex:
                    msg = 'could not resolve dtype for %s \'%s\'' % (type(container).__name__, container.name)
                    raise Exception(msg) from ex
                builder = DatasetBuilder(name, bldr_data, parent=parent, source=source, dtype=dtype)
            else:
                # a regular dtype
                if spec_dtype is None and self.__is_reftype(container.data):
                    # an unspecified dtype and we were given references
                    bldr_data = list()
                    for d in container.data:
                        if d is None:
                            bldr_data.append(None)
                        else:
                            bldr_data.append(ReferenceBuilder(manager.build(d)))
                    builder = DatasetBuilder(name, bldr_data, parent=parent, source=source,
                                             dtype='object')
                else:
                    # a dataset that has no references, pass the conversion off to
                    # the convert_dtype method
                    try:
                        bldr_data, dtype = self.convert_dtype(spec, container.data)
                    except Exception as ex:
                        msg = 'could not resolve dtype for %s \'%s\'' % (type(container).__name__, container.name)
                        raise Exception(msg) from ex
                    builder = DatasetBuilder(name, bldr_data, parent=parent, source=source, dtype=dtype)
        self.__add_attributes(builder, self.__spec.attributes, container, manager, source)
        return builder

    def __check_dset_spec(self, orig, ext):
        """
        Check a dataset spec against a refining spec to see which dtype and shape should be used
        """
        dtype = orig.dtype
        shape = orig.shape
        spec = orig
        if ext is not None:
            if ext.dtype is not None:
                dtype = ext.dtype
            if ext.shape is not None:
                shape = ext.shape
            spec = ext
        return dtype, shape, spec

    def __is_reftype(self, data):
        tmp = data
        while hasattr(tmp, '__len__') and not isinstance(tmp, (AbstractContainer, str, bytes)):
            tmptmp = None
            for t in tmp:
                # In case of a numeric array stop the iteration at the first element to avoid long-running loop
                if isinstance(t, (int, float, complex, bool)):
                    break
                if hasattr(t, '__len__') and len(t) > 0 and not isinstance(t, (AbstractContainer, str, bytes)):
                    tmptmp = tmp[0]
                    break
            if tmptmp is not None:
                break
            else:
                if len(tmp) == 0:
                    tmp = None
                else:
                    tmp = tmp[0]
        if isinstance(tmp, AbstractContainer):
            return True
        else:
            return False

    def __get_ref_builder(self, dtype, shape, container, manager):
        bldr_data = None
        if dtype.is_region():
            if shape is None:
                if not isinstance(container, DataRegion):
                    msg = "'container' must be of type DataRegion if spec represents region reference"
                    raise ValueError(msg)
                bldr_data = RegionBuilder(container.region, manager.build(container.data))
            else:
                bldr_data = list()
                for d in container.data:
                    bldr_data.append(RegionBuilder(d.slice, manager.build(d.target)))
        else:
            if isinstance(container, Data):
                bldr_data = list()
                if self.__is_reftype(container.data):
                    for d in container.data:
                        bldr_data.append(ReferenceBuilder(manager.build(d)))
            else:
                bldr_data = ReferenceBuilder(manager.build(container))
        return bldr_data

    def __is_null(self, item):
        if item is None:
            return True
        else:
            if any(isinstance(item, t) for t in (list, tuple, dict, set)):
                return len(item) == 0
        return False

    def __add_attributes(self, builder, attributes, container, build_manager, source):
        for spec in attributes:
            if spec.value is not None:
                attr_value = spec.value
            else:
                attr_value = self.get_attr_value(spec, container, build_manager)
                if attr_value is None:
                    attr_value = spec.default_value

            attr_value = self.__check_ref_resolver(attr_value)
            if isinstance(spec.dtype, RefSpec):
                if not self.__is_reftype(attr_value):
                    if attr_value is None:
                        msg = "object of data_type %s not found on %s '%s'" % \
                              (spec.dtype.target_type, type(container).__name__, container.name)
                    else:
                        msg = "invalid type for reference '%s' (%s) - "\
                              "must be AbstractContainer" % (spec.name, type(attr_value))
                    raise ValueError(msg)
                target_builder = build_manager.build(attr_value, source=source)
                attr_value = ReferenceBuilder(target_builder)
            else:
                if attr_value is not None:
                    try:
                        attr_value, attr_dtype = self.convert_dtype(spec, attr_value)
                    except Exception as ex:
                        msg = 'could not convert %s for %s %s' % (spec.name, type(container).__name__, container.name)
                        raise Exception(msg) from ex

            # do not write empty or null valued objects
            if attr_value is None:
                if spec.required:
                    msg = "attribute '%s' for '%s' (%s)"\
                                  % (spec.name, builder.name, self.spec.data_type_def)
                    warnings.warn(msg, MissingRequiredWarning)
                continue

            builder.set_attribute(spec.name, attr_value)

    def __add_links(self, group_builder, links, container, build_manager, source):
        for spec in links:
            attr_value = self.get_attr_value(spec, container, build_manager)
            if not attr_value:
                continue
            self.__add_containers(group_builder, spec, attr_value, build_manager, source, container)

    def __add_datasets(self, group_builder, datasets, container, build_manager, source):
        for spec in datasets:
            attr_value = self.get_attr_value(spec, container, build_manager)
            if attr_value is None:
                continue
            attr_value = self.__check_ref_resolver(attr_value)
            if isinstance(attr_value, DataIO) and attr_value.data is None:
                continue
            if isinstance(attr_value, Builder):
                group_builder.set_builder(attr_value)
            elif spec.data_type_def is None and spec.data_type_inc is None:
                # a non-Container/Data dataset, e.g. a float or nd-array
                if spec.name in group_builder.datasets:
                    dataset_builder = group_builder.datasets[spec.name]
                else:
                    try:
                        # convert the given data values to the spec dtype
                        data, dtype = self.convert_dtype(spec, attr_value)
                    except Exception as ex:
                        msg = ('could not convert \'%s\' for %s \'%s\''
                               % (spec.name, type(container).__name__, container.name))
                        raise Exception(msg) from ex
                    dataset_builder = group_builder.add_dataset(spec.name, data, dtype=dtype, dims=spec.dims,
                                                                coords=spec.coords)
                self.__add_attributes(dataset_builder, spec.attributes, container, build_manager, source)
            else:
                # a Container/Data dataset, e.g. a VectorData
                self.__add_containers(group_builder, spec, attr_value, build_manager, source, container)

        # resolve dims
        for spec in datasets:
            if spec.name is None:
                # TODO currently only named dataset specs can have dimensions. need to handle VectorData case where
                # name is not known
                continue

            if spec.coords:
                for coord_spec in spec.coords:
                    dataset_builder = group_builder.datasets[spec.name]  # all named dataset builders should exist now
                    # TODO revise me
                    dim_dataset_builder = group_builder.datasets.get(coord_spec.coord, None)
                    if dim_dataset_builder is None:
                        raise ValueError("Coordinate '%s' for spec '%s' not found in group '%s'"
                                         % (coord_spec.coord, spec.name, group_builder.name))
                    if coord_spec.type == 'coord':
                        dataset_builder.coords[coord_spec.label] = dim_dataset_builder
                    else:
                        raise Exception('TODO')

    def __add_groups(self, group_builder, groups, container, build_manager, source):
        for spec in groups:
            if spec.data_type_def is None and spec.data_type_inc is None:
                # we don't need to get attr_name since any named
                # group does not have the concept of value
                subgroup_builder = group_builder.groups.get(spec.name)
                if subgroup_builder is None:
                    subgroup_builder = GroupBuilder(spec.name, source=source)
                self.__add_attributes(subgroup_builder, spec.attributes, container, build_manager, source)
                self.__add_datasets(subgroup_builder, spec.datasets, container, build_manager, source)

                # handle subgroups that are not Containers
                attr_name = self.get_attribute(spec)
                if attr_name is not None:
                    attr_value = self.get_attr_value(spec, container, build_manager)
                    if any(isinstance(attr_value, t) for t in (list, tuple, set, dict)):
                        it = iter(attr_value)
                        if isinstance(attr_value, dict):
                            it = iter(attr_value.values())
                        for item in it:
                            if isinstance(item, Container):
                                self.__add_containers(subgroup_builder, spec, item, build_manager, source, container)
                self.__add_groups(subgroup_builder, spec.groups, container, build_manager, source)
                empty = subgroup_builder.is_empty()
                if not empty or (empty and isinstance(spec.quantity, int)):
                    if subgroup_builder.name not in group_builder.groups:
                        group_builder.set_group(subgroup_builder)
            else:
                if spec.data_type_def is not None:
                    attr_name = self.get_attribute(spec)
                    if attr_name is not None:
                        attr_value = getattr(container, attr_name, None)
                        if attr_value is not None:
                            self.__add_containers(group_builder, spec, attr_value, build_manager, source, container)
                else:
                    attr_name = self.get_attribute(spec)
                    attr_value = self.get_attr_value(spec, container, build_manager)
                    if attr_value is not None:
                        self.__add_containers(group_builder, spec, attr_value, build_manager, source, container)

    def __add_containers(self, group_builder, spec, value, build_manager, source, parent_container):
        if isinstance(value, AbstractContainer):
            if value.parent is None:
                msg = "'%s' (%s) for '%s' (%s)"\
                              % (value.name, getattr(value, self.spec.type_key()),
                                 group_builder.name, self.spec.data_type_def)
                warnings.warn(msg, OrphanContainerWarning)
            if value.modified:                   # writing a new container
                if isinstance(spec, BaseStorageSpec):
                    rendered_obj = build_manager.build(value, source=source, spec_ext=spec)
                else:
                    rendered_obj = build_manager.build(value, source=source)
                # use spec to determine what kind of HDF5
                # object this AbstractContainer corresponds to
                if isinstance(spec, LinkSpec) or value.parent is not parent_container:
                    name = spec.name
                    group_builder.set_link(LinkBuilder(rendered_obj, name, group_builder))
                elif isinstance(spec, DatasetSpec):
                    if rendered_obj.dtype is None and spec.dtype is not None:
                        val, dtype = self.convert_dtype(spec, rendered_obj.data)
                        rendered_obj.dtype = dtype
                    group_builder.set_dataset(rendered_obj)
                else:
                    group_builder.set_group(rendered_obj)
            elif value.container_source:        # make a link to an existing container
                if value.container_source != parent_container.container_source or\
                   value.parent is not parent_container:
                    if isinstance(spec, BaseStorageSpec):
                        rendered_obj = build_manager.build(value, source=source, spec_ext=spec)
                    else:
                        rendered_obj = build_manager.build(value, source=source)
                    group_builder.set_link(LinkBuilder(rendered_obj, name=spec.name, parent=group_builder))
            else:
                raise ValueError("Found unmodified AbstractContainer with no source - '%s' with parent '%s'" %
                                 (value.name, parent_container.name))
        else:
            if any(isinstance(value, t) for t in (list, tuple)):
                values = value
            elif isinstance(value, dict):
                values = value.values()
            else:
                msg = ("received %s, expected AbstractContainer - 'value' "
                       "must be an AbstractContainer a list/tuple/dict of "
                       "AbstractContainers if 'spec' is a GroupSpec")
                raise ValueError(msg % value.__class__.__name__)
            for container in values:
                if container:
                    self.__add_containers(group_builder, spec, container, build_manager, source, parent_container)

    def __get_subspec_values(self, builder, spec, manager):
        ret = dict()
        # First get attributes
        attributes = builder.attributes
        for attr_spec in spec.attributes:
            attr_val = attributes.get(attr_spec.name)
            if attr_val is None:
                continue
            if isinstance(attr_val, (GroupBuilder, DatasetBuilder)):
                ret[attr_spec] = manager.construct(attr_val)
            elif isinstance(attr_val, RegionBuilder):
                raise ValueError("RegionReferences as attributes is not yet supported")
            elif isinstance(attr_val, ReferenceBuilder):
                ret[attr_spec] = manager.construct(attr_val.builder)
            else:
                ret[attr_spec] = attr_val
        if isinstance(spec, GroupSpec):
            if not isinstance(builder, GroupBuilder):
                raise ValueError("__get_subspec_values - must pass GroupBuilder with GroupSpec")
            # first aggregate links by data type and separate them
            # by group and dataset
            groups = dict(builder.groups)             # make a copy so we can separate links
            datasets = dict(builder.datasets)         # make a copy so we can separate links
            links = builder.links
            link_dt = dict()
            for link_builder in links.values():
                target = link_builder.builder
                if isinstance(target, DatasetBuilder):
                    datasets[link_builder.name] = target
                else:
                    groups[link_builder.name] = target
                dt = manager.get_builder_dt(target)
                if dt is not None:
                    link_dt.setdefault(dt, list()).append(target)
            # now assign links to their respective specification
            for subspec in spec.links:
                if subspec.name is not None and subspec.name in links:
                    ret[subspec] = manager.construct(links[subspec.name].builder)
                else:
                    sub_builder = link_dt.get(subspec.target_type)
                    if sub_builder is not None:
                        ret[subspec] = self.__flatten(sub_builder, subspec, manager)
            # now process groups and datasets
            self.__get_sub_builders(groups, spec.groups, manager, ret)
            self.__get_sub_builders(datasets, spec.datasets, manager, ret)
        elif isinstance(spec, DatasetSpec):
            if not isinstance(builder, DatasetBuilder):
                raise ValueError("__get_subspec_values - must pass DatasetBuilder with DatasetSpec")
            ret[spec] = self.__check_ref_resolver(builder.data)
        return ret

    @staticmethod
    def __check_ref_resolver(data):
        """
        Check if this dataset is a reference resolver, and invert it if so.
        """
        if isinstance(data, ReferenceResolver):
            return data.invert()
        return data

    def __get_sub_builders(self, sub_builders, subspecs, manager, ret):
        # index builders by data_type
        builder_dt = dict()
        for g in sub_builders.values():
            dt = manager.get_builder_dt(g)
            ns = manager.get_builder_ns(g)
            if dt is None or ns is None:
                continue
            for parent_dt in manager.namespace_catalog.get_hierarchy(ns, dt):
                builder_dt.setdefault(parent_dt, list()).append(g)
        for subspec in subspecs:
            # first get data type for the spec
            if subspec.data_type_def is not None:
                dt = subspec.data_type_def
            elif subspec.data_type_inc is not None:
                dt = subspec.data_type_inc
            else:
                dt = None
            # use name if we can, otherwise use data_data
            if subspec.name is None:
                sub_builder = builder_dt.get(dt)
                if sub_builder is not None:
                    sub_builder = self.__flatten(sub_builder, subspec, manager)
                    ret[subspec] = sub_builder
            else:
                sub_builder = sub_builders.get(subspec.name)
                if sub_builder is None:
                    continue
                if dt is None:
                    # recurse
                    ret.update(self.__get_subspec_values(sub_builder, subspec, manager))
                else:
                    ret[subspec] = manager.construct(sub_builder)

    def __flatten(self, sub_builder, subspec, manager):
        tmp = [manager.construct(b) for b in sub_builder]
        if len(tmp) == 1 and not subspec.is_many():
            tmp = tmp[0]
        return tmp

    @docval({'name': 'builder', 'type': (DatasetBuilder, GroupBuilder),
             'doc': 'the builder to construct the AbstractContainer from'},
            {'name': 'manager', 'type': BuildManager, 'doc': 'the BuildManager for this build'},
            {'name': 'parent', 'type': (Proxy, AbstractContainer),
             'doc': 'the parent AbstractContainer/Proxy for the AbstractContainer being built', 'default': None})
    def construct(self, **kwargs):
        ''' Construct an AbstractContainer from the given Builder '''
        builder, manager, parent = getargs('builder', 'manager', 'parent', kwargs)
        cls = manager.get_cls(builder)
        # gather all subspecs
        subspecs = self.__get_subspec_values(builder, self.spec, manager)
        # get the constructor argument that each specification corresponds to
        const_args = dict()
        # For Data container classes, we need to populate the data constructor argument since
        # there is no sub-specification that maps to that argument under the default logic
        if issubclass(cls, Data):
            if not isinstance(builder, DatasetBuilder):
                raise ValueError('Can only construct a Data object from a DatasetBuilder - got %s' % type(builder))
            const_args['data'] = self.__check_ref_resolver(builder.data)
        for subspec, value in subspecs.items():
            const_arg = self.get_const_arg(subspec)
            if const_arg is not None:
                if isinstance(subspec, BaseStorageSpec) and subspec.is_many():
                    existing_value = const_args.get(const_arg)
                    if isinstance(existing_value, list):
                        value = existing_value + value
                const_args[const_arg] = value
        # build kwargs for the constructor
        kwargs = dict()
        for const_arg in get_docval(cls.__init__):
            argname = const_arg['name']
            override = self.__get_override_carg(argname, builder, manager)
            if override is not None:
                val = override
            elif argname in const_args:
                val = const_args[argname]
            else:
                continue
            kwargs[argname] = val
        try:
            obj = cls.__new__(cls, container_source=builder.source, parent=parent,
                              object_id=builder.attributes.get(self.__spec.id_key()))
            obj.__init__(**kwargs)
        except Exception as ex:
            msg = 'Could not construct %s object' % (cls.__name__,)
            raise Exception(msg) from ex

        # add dimension coordinates after object construction
        datasets = getattr(self.__spec, 'datasets', None)
        if datasets is not None:
            for dataset in datasets:
                dims = getattr(dataset, 'dims', None)
                if dims is not None:
                    for dim_coord in dataset.dims:
                        # TODO axis
                        if dim_coord.type == 'coord':
                            obj.set_dim_coord(data_name=dataset.name, axis=0, label=dim_coord.label,
                                              coord=dim_coord.coord)
                        else:  # TODO
                            pass
        return obj

    @docval({'name': 'container', 'type': AbstractContainer,
             'doc': 'the AbstractContainer to get the Builder name for'})
    def get_builder_name(self, **kwargs):
        '''Get the name of a Builder that represents a AbstractContainer'''
        container = getargs('container', kwargs)
        if self.__spec.name not in (NAME_WILDCARD, None):
            ret = self.__spec.name
        else:
            if container.name is None:
                if self.__spec.default_name is not None:
                    ret = self.__spec.default_name
                else:
                    msg = 'Unable to determine name of container type %s' % self.__spec.data_type_def
                    raise ValueError(msg)
            else:
                ret = container.name
        return ret
