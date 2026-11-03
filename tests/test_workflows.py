"""Validate saved workflow documents; ordinary YAML loaders accept duplicate keys."""
from pathlib import Path
import unittest
import yaml


class UniqueKeysLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader,node,deep=False):
    result={}
    for key_node,value_node in node.value:
        key=loader.construct_object(key_node,deep=deep)
        if key in result:
            raise ValueError(f'duplicate workflow YAML key at line {key_node.start_mark.line+1}: {key}')
        result[key]=loader.construct_object(value_node,deep=deep)
    return result


UniqueKeysLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,unique_mapping)


class WorkflowTests(unittest.TestCase):
    def test_repository_workflows_have_no_duplicate_keys(self):
        folder=Path(__file__).resolve().parents[1]/'.github/workflows'
        paths=list(folder.glob('*.yml'))+list(folder.glob('*.yaml'))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path.name):
                value=yaml.load(path.read_text(),Loader=UniqueKeysLoader)
                self.assertIsInstance(value.get('jobs'),dict)

    def test_accidentally_concatenated_workflows_are_rejected(self):
        text='name: scan\non: workflow_dispatch\njobs: {}\n'
        with self.assertRaisesRegex(ValueError,'duplicate workflow YAML key'):
            yaml.load(text+text,Loader=UniqueKeysLoader)
