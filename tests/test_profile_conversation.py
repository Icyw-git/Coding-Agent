from scripts.profile_conversation import summarize


def test_profile_summary_aggregates_latency_cache_and_tools(tmp_path):
    from session_store import SessionEventStore

    store = SessionEventStore(tmp_path, session_id='profile')
    store.append('usage', prompt=100, cached=80, prepare_ms=2.0,
                 request_ms=500.0, ttfb_ms=100.0, stream_ms=400.0)
    store.append('usage', prompt=100, cached=20, prepare_ms=4.0,
                 request_ms=900.0, ttfb_ms=300.0, stream_ms=600.0)
    store.append('tool_profile', duration_ms=30.0, tool_count=2)

    result = summarize([store.path])

    assert result['model_calls'] == 2
    assert result['profiled_calls'] == 2
    assert result['weighted_cache_hit_rate'] == 0.5
    assert result['low_hit_calls'] == 1
    assert result['timings_ms']['request_ms']['p50'] == 700.0
    assert result['timings_ms']['ttfb_ms']['p95'] == 100.0
    assert result['tool_batches'] == 1
    assert result['tool_calls'] == 2
