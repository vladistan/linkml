import pytest

from linkml.generators.markdowndatadictgen import MarkdownDataDictGen
from linkml.generators.markdowngen import MarkdownGenerator

# from tests.test_generators.environment import env

# SCHEMA = env.input_path("personinfo.yaml")
# OUT_PATH = env.expected_path("personinfo.relational.yaml")
# RSCHEMA_EXPANDED = env.expected_path("personinfo.relational.expanded.yaml")
# OUT_DDL = env.expected_path("personinfo.ddl.sql")
# META_OUT_DDL = env.expected_path("meta.ddl.sql")
# SQLDDLLOG = env.expected_path("personinfo.sql.log")
# DB = env.expected_path("personinfo.db")
DUMMY_CLASS = "dummy class"


@pytest.fixture
def schema(input_path):
    return str(input_path("personinfo.yaml"))


@pytest.fixture
def kitch_sink_schema(input_path):
    return str(input_path("kitchen_sink.yaml"))



def test_datadict_gen(schema, tmp_path):
    gen = MarkdownGenerator(schema)
    gen.serialize(directory=tmp_path)


def test_datadict_real(schema, tmp_path):
    gen = MarkdownDataDictGen(schema)
    t_dir = "/Users/vlad/Proj/mine/thesis/linkml/tgen"
    gen.serialize(output=t_dir + "/personinfo.md")


def test_datadict_kitchensink(kitch_sink_schema):
    gen = MarkdownDataDictGen(kitch_sink_schema)
    t_dir = "/Users/vlad/Proj/mine/thesis/linkml/tgen"
    gen.serialize(output=t_dir + "/kitchen_sink.md")
