from scripts.profile_conversation import summarize


def test_profile_summary_aggregates_latency_cache_and_tools(tmp_path):
    from session_store import SessionEventStore

    store = SessionEventStore(tmp_path, session_id='profile')
    store.append('usage', prompt=100, cached=80, prepare_ms=2.0,
                 request_ms=500.0, ttfb_ms=100.0, stream_ms=400.0,
                 cache_prefix_status='appended')
    store.append('usage', prompt=100, cached=20, prepare_ms=4.0,
                 request_ms=900.0, ttfb_ms=300.0, stream_ms=600.0,
                 cache_prefix_status='rewritten')
    store.append('model_request_failed', request_ms=1200.0,
                 provider_status=402, error_type='APIStatusError')
    store.append('model_request_completed', request_ms=400.0, ttfb_ms=50.0,
                 usage_available=False)
    store.append('tool_profile', duration_ms=30.0, tool_count=2, status='ok')
    store.append('tool_profile', duration_ms=20.0, tool_count=1,
                 status='error', error_type='RuntimeError')

    result = summarize([store.path])

    assert result['model_calls'] == 2
    assert result['successful_model_requests'] == 3
    assert result['successful_requests_without_usage'] == 1
    assert result['profiled_calls'] == 3
    assert result['weighted_cache_hit_rate'] == 0.5
    assert result['low_hit_calls'] == 1
    assert result['cache_prefix_status'] == {'appended': 1, 'rewritten': 1}
    assert result['low_hit_by_prefix_status'] == {'rewritten': 1}
    assert result['failed_model_requests'] == 1
    assert result['failures_by_provider_status'] == {'402': 1}
    assert result['failed_request_ms']['p50'] == 1200.0
    assert result['timings_ms']['request_ms']['p50'] == 500.0
    assert result['timings_ms']['ttfb_ms']['p95'] == 100.0
    assert result['tool_batches'] == 2
    assert result['tool_batches_with_errors'] == 1
    assert result['tool_calls'] == 3
