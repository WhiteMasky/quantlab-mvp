from quantlab.research import candidate_specs


def test_two_hypotheses_and_complete_bounded_grid():
    specs = candidate_specs()
    assert [spec['strategy'] for spec in specs] == ['trend', 'momentum']
    for spec in specs:
        assert len(spec['parameter_grid']) == 2
        assert spec['synthetic_policy']['passed'] is False
        assert not spec['evaluation_protocol']['holdout_optimization']
        assert 'total return' in spec['evaluation_protocol']['selection_metric']
    specs[0]['parameter_grid'].clear()
    assert len(candidate_specs()[0]['parameter_grid']) == 2
