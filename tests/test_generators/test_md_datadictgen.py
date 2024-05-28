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


def test_datadict_gen(schema, tmp_path):

    gen = MarkdownGenerator(schema)
    gen.serialize(directory=tmp_path)


def test_datadict_real(schema, tmp_path):

    gen = MarkdownDataDictGen(schema)
    t_dir = "/Users/vlad/Proj/mine/thesis/linkml/tmp_gen"

    import os

    if not os.path.exists(t_dir):
        os.makedirs(t_dir)
    gen.serialize(directory=t_dir)
