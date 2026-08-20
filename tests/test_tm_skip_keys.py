from hanpatch import tm


def test_profile_declared_skip_keys_are_left_to_their_runtime_owner(monkeypatch):
    monkeypatch.setattr(tm.config, 'prof', lambda key: ['off-firmware'] if key == 'skip_keys' else None)
    assert tm.is_skip('firmware-rendered dialog', 'off-firmware')
    assert not tm.is_skip('ordinary game text', 'off-game')
