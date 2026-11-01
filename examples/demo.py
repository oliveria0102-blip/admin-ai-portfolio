"""Offline component verification using a fresh, preserved synthetic workspace."""
from pathlib import Path
import importlib.util
import json
import uuid

ROOT = Path(__file__).resolve().parents[1]

def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def main():
    stores = load('task_state_store', 'core/adapters/task_state_store.py')
    effects = load('side_effect_classifier', 'core/policies/side_effect_classifier.py')
    output = ROOT / 'work' / ('demo-' + uuid.uuid4().hex[:12])
    output.mkdir(parents=True, exist_ok=False)
    store = stores.SqliteStore(str(output))
    initial = store.create_task({'task_id': 'SYNTHETIC-TASK-001', 'status': 'ACTIVE'})
    updated = store.compare_and_update(initial['task_id'], 1, lambda state: {**state, 'example': 'reviewed'})
    stale = store.compare_and_update(initial['task_id'], 1, lambda state: state)
    assert updated['state_version'] == 2
    assert stale == stores.STATE_CONFLICT
    assert store.get_state(initial['task_id'])['example'] == 'reviewed'
    cases = {'git status': 'READ_ONLY', 'mkdir example': 'LOCAL_WORKING_WRITE',
             'curl https://example.invalid': 'NETWORK_ACCESS', 'git push': 'EXTERNAL_SIDE_EFFECT'}
    observed = {command: effects.classify_bash(command) for command in cases}
    assert observed == cases, observed
    store._conn.close()
    result = {'data_class': 'SYNTHETIC_COMPONENT_CHECK', 'state_version_after_update': 2,
              'stale_write': stale, 'command_classification': observed,
              'network_calls': 0, 'shell_commands_executed': 0}
    (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print('Evidence directory:', output)

if __name__ == '__main__':
    main()
