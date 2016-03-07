import threading

from hazelcast import util
from hazelcast.exception import HazelcastSerializationError
from hazelcast.serialization import bits
from hazelcast.serialization.portable.classdef import ClassDefinition, ClassDefinitionBuilder, FieldType, FieldDefinition
from hazelcast.serialization.portable.writer import ClassDefinitionWriter


class PortableContext(object):
    def __init__(self, serialization_service, portable_version):
        self.serialization_service = serialization_service
        self.portable_version = portable_version
        self._class_defs = dict()  # {factory_id:ClassDefinitionContext}

    def get_portable_version(self):
        return self.portable_version

    def get_class_version(self, factory_id, class_id):
        return self._get_class_def_context(factory_id).get_class_version(class_id)

    def set_class_version(self, factory_id, class_id, version):
        self._get_class_def_context(factory_id).set_class_version(class_id, version)

    def lookup_class_definition(self, factory_id, class_id, version):
        return self._get_class_def_context(factory_id).lookup(class_id, version)

    def read_class_definition(self, data_in, factory_id, class_id, version):
        register = True
        builder = ClassDefinitionBuilder(factory_id, class_id, version)

        # final position after portable is read
        data_in.read_int()

        # field count
        field_count = data_in.read_int()
        offset = data_in.position()
        for i in xrange(0, field_count):
            pos = data_in.read_int(offset + i * bits.INT_SIZE_IN_BYTES)
            data_in.set_position(pos)

            _len = data_in.read_short()
            field_name = bytearray(_len)
            data_in.read_into(field_name)

            ft_byte = data_in.read_byte()
            field_type = FieldType.reverse[ft_byte]
            if field_type == FieldType.PORTABLE:
                # is null
                if data_in.read_boolean():
                    register = False
                field_factory_id = data_in.read_int()
                field_class_id = data_in.read_int()

                # TODO: what there's a null inner Portable field
                if register:
                    field_version = data_in.read_int()
                    self.read_class_definition(data_in, field_factory_id, field_class_id, field_version)
            elif type == FieldType.PORTABLE_ARRAY:
                k = data_in.read_int()
                field_factory_id = data_in.read_int()
                field_class_id = data_in.read_int()

                # TODO: what there's a null inner Portable field
                if k > 0:
                    p = data_in.read_int()
                    data_in.set_position(p)
                    field_version = data_in.read_int()
                    self.read_class_definition(data_in, field_factory_id, field_class_id, field_version)
                else:
                    register = False
            else:
                raise HazelcastSerializationError("Malformed portable class definition")
            builder.add_field_def(FieldDefinition(i, field_name, field_type, field_factory_id, field_class_id))
        class_def = builder.build()
        if register:
            class_def = self.register_class_definition(class_def)
        return class_def

    def register_class_definition(self, class_definition):
        return self._get_class_def_context(class_definition.factory_id).register(class_definition)

    def lookup_or_register_class_definition(self, portable):
        fid = portable.get_factory_id()
        cid = portable.get_class_id()
        portable_version = util.get_portable_version(portable, self.portable_version)
        class_def = self.lookup_class_definition(fid, cid, portable_version)

        if class_def is None:
            writer = ClassDefinitionWriter(self, fid, cid, portable_version)
            portable.write_portable(writer)
            class_def = writer.register_and_get()

        return class_def

    def get_field_definition(self, class_def, name):
        fd = class_def.get_field(name)
        if fd is None:
            field_names = name.split(".")
            if len(field_names) > 1:
                current_class_def = class_def
                for i in xrange(0, len(field_names)):
                    fname = field_names[i]
                    fd = current_class_def.get_field(fname)
                    if i == len(field_names) - 1:
                        break
                    if fd is None:
                        raise ValueError("Unknown field: {}".format(fname))
                    current_class_def = self.lookup_class_definition(fd.factory_id, fd.class_id, current_class_def.version)
                    if current_class_def is None:
                        raise ValueError("Not a registered Portable field: {}".format(fd))
        return fd

    def _get_class_def_context(self, factory_id):
        try:
            return self._class_defs[factory_id]
        except KeyError:
            return ClassDefinitionContext(factory_id)


class ClassDefinitionContext(object):
    def __init__(self, factory_id, version):
        self._factory_id = factory_id
        self._version = version
        self._versioned_definitions = {}  # (class_id, version) : ClassDefinition
        self._current_class_versions = {}  # class_id:version
        self._lock = threading.RLock()

    # int getClassVersion(int factoryId, int classId);
    def get_class_version(self, class_id):
        return self._current_class_versions.get(class_id, -1)

    # void setClassVersion(int factoryId, int classId, int version);
    def set_class_version(self, class_id, version):
        try:
            current_version = self._current_class_versions[class_id]
            if current_version != version:
                raise ValueError("Class-id: {} is already registered!".format(class_id))
        except KeyError:
            self._current_class_versions[class_id] = version

    def lookup(self, class_id, version):
        return self._versioned_definitions.get((class_id, version), None)

    def register(self, class_def):
        with self._lock:
            if class_def is None:
                return None
            if class_def.factory_id != self._factory_id:
                raise HazelcastSerializationError("Invalid factory-id! {} -> {}".format(self._factory_id, class_def))
            if isinstance(class_def, ClassDefinition):
                class_def.set_version_if_not_set(self._version)
            combined_key = (class_def.class_id, class_def.version)
            if not self._versioned_definitions.has_key(combined_key):
                self._versioned_definitions[combined_key] = class_def
                return class_def
            current_class_def = self._versioned_definitions[combined_key]
            if isinstance(current_class_def, ClassDefinition):
                if current_class_def != class_def:
                    raise HazelcastSerializationError("Incompatible class-definitions with same class-id: {} vs {}"
                                                      .format(class_def, current_class_def))
                return current_class_def
            self._versioned_definitions[combined_key] = class_def
            return class_def
