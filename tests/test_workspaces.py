"""工作区登记表：只负责发现与命名，不负责授权。"""

import json

import pytest

import workspaces
from workspace_choices import find_workspace, workspace_choices


def test_default_path_prefers_env_override(tmp_path, monkeypatch):
    monkeypatch.delenv('HARNESS_WORKSPACE_FILE', raising=False)
    assert (workspaces.default_path(tmp_path / 'sessions')
            == tmp_path / 'workspaces.json')
    monkeypatch.setenv('HARNESS_WORKSPACE_FILE', str(tmp_path / 'custom.json'))
    assert workspaces.default_path(tmp_path / 'sessions') == tmp_path / 'custom.json'


def test_add_list_and_remove_roundtrip(tmp_path):
    file = tmp_path / 'workspaces.json'
    alpha = tmp_path / 'alpha'
    beta = tmp_path / 'beta'
    alpha.mkdir()
    beta.mkdir()

    first = workspaces.add_workspace(alpha, file=file)
    second = workspaces.add_workspace(beta, name='Second', file=file)

    listing = workspaces.list_workspaces(file=file)
    assert [entry['path'] for entry in listing] == [str(beta.resolve()),
                                                    str(alpha.resolve())]
    assert listing[0]['name'] == 'Second'
    assert listing[0]['id'] == second['id']
    assert first['id'] != second['id']
    document = json.loads(file.read_text(encoding='utf-8'))
    assert document['version'] == 1
    assert len(document['workspaces']) == 2

    removed = workspaces.remove_workspace('Second', file=file)
    assert removed['path'] == str(beta.resolve())
    assert [entry['path'] for entry in workspaces.list_workspaces(file=file)] \
        == [str(alpha.resolve())]


def test_adding_the_same_directory_refreshes_its_name(tmp_path):
    file = tmp_path / 'workspaces.json'
    project = tmp_path / 'project'
    project.mkdir()

    first = workspaces.add_workspace(project, name='Before', file=file)
    second = workspaces.add_workspace(f"{project}{'/'}", name='After', file=file)

    assert second['id'] == first['id']
    assert second['name'] == 'After'
    assert len(workspaces.list_workspaces(file=file)) == 1


def test_add_rejects_missing_directory_and_leaves_no_file(tmp_path):
    file = tmp_path / 'workspaces.json'
    with pytest.raises(ValueError, match='does not exist'):
        workspaces.add_workspace(tmp_path / 'missing', file=file)
    assert not file.exists()


def test_corrupt_registry_is_tolerated_and_can_be_rebuilt(tmp_path):
    file = tmp_path / 'workspaces.json'
    file.write_text('{not json', encoding='utf-8')
    assert workspaces.list_workspaces(file=file) == []

    project = tmp_path / 'project'
    project.mkdir()
    workspaces.add_workspace(project, file=file)
    assert [entry['path'] for entry in workspaces.list_workspaces(file=file)] \
        == [str(project.resolve())]


def test_hand_edited_duplicates_collapse_to_the_latest_entry(tmp_path):
    file = tmp_path / 'workspaces.json'
    project = tmp_path / 'project'
    project.mkdir()
    document = {'version': 1, 'workspaces': [
        {'path': str(project), 'name': 'Old', 'last_used_at': 1},
        {'path': str(project / '.'), 'name': 'New', 'last_used_at': 5},
    ]}
    file.write_text(json.dumps(document), encoding='utf-8')

    listing = workspaces.list_workspaces(file=file)
    assert len(listing) == 1
    assert listing[0]['name'] == 'New'
    assert listing[0]['last_used_at'] == 5


def test_remember_workspace_never_blocks_on_bad_paths(tmp_path):
    file = tmp_path / 'workspaces.json'
    assert workspaces.remember_workspace(tmp_path / 'missing', file=file) is None
    project = tmp_path / 'project'
    project.mkdir()
    assert workspaces.remember_workspace(project, file=file)['path'] \
        == str(project.resolve())


def test_remove_reports_missing_and_ambiguous_selectors(tmp_path):
    file = tmp_path / 'workspaces.json'
    alpha = tmp_path / 'alpha'
    beta = tmp_path / 'beta'
    alpha.mkdir()
    beta.mkdir()
    workspaces.add_workspace(alpha, name='same', file=file)
    workspaces.add_workspace(beta, name='same', file=file)

    with pytest.raises(ValueError, match='多个'):
        workspaces.remove_workspace('same', file=file)
    with pytest.raises(KeyError):
        workspaces.remove_workspace('nope', file=file)


def _rows():
    return [
        {'id': 'aaa111', 'name': 'Alpha', 'path': r'C:\work\alpha'},
        {'id': 'bbb222', 'name': 'Beta', 'path': r'C:\work\beta'},
    ]


def test_find_workspace_accepts_index_path_name_and_id():
    rows = _rows()
    assert find_workspace(rows, '2')['id'] == 'bbb222'
    assert find_workspace(rows, r'C:\work\beta')['id'] == 'bbb222'
    assert find_workspace(rows, 'beta')['id'] == 'bbb222'
    assert find_workspace(rows, 'BBB222')['id'] == 'bbb222'
    assert find_workspace(rows, r'"C:\work\alpha"')['id'] == 'aaa111'


@pytest.mark.parametrize('selector', ['', '0', '3', 'gamma'])
def test_find_workspace_rejects_invalid_selectors(selector):
    with pytest.raises(ValueError):
        find_workspace(_rows(), selector)


def test_find_workspace_rejects_ambiguous_names():
    rows = _rows()
    rows[1]['name'] = 'Alpha'
    with pytest.raises(ValueError, match='多个'):
        find_workspace(rows, 'alpha')


def test_choices_merge_registered_and_recent_without_duplicates(tmp_path):
    current = tmp_path / 'current'
    recent = tmp_path / 'recent'
    saved = tmp_path / 'saved'
    for directory in (current, recent, saved):
        directory.mkdir()

    rows = [{'workspace': str(recent), 'updated': 5},
            {'workspace': str(saved / '.'), 'updated': 1}]
    registry = [{'path': str(saved), 'name': 'Saved Project', 'last_used_at': 10}]

    choices = workspace_choices(rows, current, saved=registry,
                                allowed_roots=[tmp_path])

    assert [choice['path'] for choice in choices] == [
        str(current), str(saved), str(recent)]
    assert choices[0]['current'] is True
    assert choices[0]['saved'] is False
    assert choices[1]['name'] == 'Saved Project'
    assert choices[1]['saved'] is True
    assert choices[1]['sessions'] == 1
    assert all(choice['exists'] and choice['allowed'] for choice in choices)


def test_registered_missing_or_unauthorized_workspaces_are_flagged(tmp_path):
    file = tmp_path / 'workspaces.json'
    missing = tmp_path / 'gone'
    outside = tmp_path.parent / 'outside-root'
    registry = [
        {'path': str(missing), 'name': 'Gone', 'last_used_at': 2},
        {'path': str(outside), 'name': 'Outside', 'last_used_at': 1},
    ]
    file.write_text(json.dumps({'version': 1, 'workspaces': registry}),
                    encoding='utf-8')

    choices = workspace_choices([], tmp_path, saved=workspaces.list_workspaces(file=file),
                                allowed_roots=[tmp_path])

    by_name = {choice['name']: choice for choice in choices}
    assert by_name['Gone']['exists'] is False
    assert by_name['Outside']['allowed'] is False
    assert by_name['Outside']['exists'] is False


def test_registry_entry_is_listed_before_it_has_any_session(tmp_path):
    registry = [{'path': str(tmp_path), 'name': 'Fresh', 'last_used_at': 3}]
    choices = workspace_choices([], tmp_path / 'elsewhere', saved=registry)
    assert len(choices) == 2
    assert choices[1]['name'] == 'Fresh'
    assert choices[1]['sessions'] == 0
