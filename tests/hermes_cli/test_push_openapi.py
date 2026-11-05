"""Pin the external v1 API; regenerate with the command in the architecture doc."""

import copy
import difflib
import json
from pathlib import Path

from fastapi import FastAPI

SNAPSHOT = Path(__file__).parents[1] / 'fixtures' / 'push_v1_openapi.json'


def generated_schema():
    from hermes_cli.web_routers.push import router
    app = FastAPI()
    app.include_router(router, prefix='/api/push/v1')
    return app.openapi()


def assert_contract(expected, actual):
    def fields(value, path='$'):
        result = {}
        if isinstance(value, dict):
            for key, child in value.items():
                result.update(fields(child, path + '/' + key.replace('~', '~0').replace('/', '~1')))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                result.update(fields(child, path + '/' + str(index)))
        else:
            result[path] = value
        return result
    old, new = fields(expected), fields(actual)
    changed = sorted(key for key in old.keys() | new.keys() if key not in old or key not in new or old[key] != new[key])
    diff = ''.join(difflib.unified_diff(
        json.dumps(expected, indent=2, sort_keys=True).splitlines(True),
        json.dumps(actual, indent=2, sort_keys=True).splitlines(True),
        fromfile='checked-in v1', tofile='generated v1'))
    assert expected == actual, ('OpenAPI contract changed at:\n' + '\n'.join(changed)
                                + '\n' + diff + '\nRegenerate: python -m '
                                'tests.hermes_cli.test_push_openapi --update')


def test_push_v1_openapi_snapshot():
    actual = generated_schema()
    assert_contract(json.loads(SNAPSHOT.read_text(encoding='utf-8-sig')), actual)


def test_snapshot_reports_renamed_property():
    import pytest
    schema = generated_schema()
    mutated = copy.deepcopy(schema)
    properties = mutated['components']['schemas']['MailboxMessage']['properties']
    properties['renamed_message'] = properties.pop('message')
    with pytest.raises(AssertionError, match='MailboxMessage/properties/renamed_message') as error:
        assert_contract(schema, mutated)
    assert 'MailboxMessage/properties/message' in str(error.value)


if __name__ == '__main__':
    import sys
    if sys.argv[1:] != ['--update']:
        raise SystemExit('Usage: python -m tests.hermes_cli.test_push_openapi --update')
    SNAPSHOT.parent.mkdir(exist_ok=True)
    SNAPSHOT.write_text(json.dumps(generated_schema(), indent=2, sort_keys=True) + '\n', encoding='utf-8')
