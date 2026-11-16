"""Unit tests for config.get_glue_client (ADR #45 cross-region support).

The helper memoizes boto3 Glue clients per region so a Lambda container
reused across invocations doesn't reinstantiate boto3 on every request.
Empty/None region returns the default-region client (same instance as
the legacy `glue` proxy), preserving the local-region fast path.
"""

import pytest


@pytest.fixture(autouse=True)
def _reset_glue_caches(mocker):
    """Reset module-level caches between tests so order doesn't matter.

    Both _glue (default-region) and _glue_by_region (per-region) must be
    cleared, otherwise the second test sees the first test's stub clients.
    """
    import config
    mocker.patch.object(config, '_glue', None)
    config._glue_by_region.clear()
    yield
    config._glue_by_region.clear()


def test_empty_region_returns_default_client(mocker):
    """Empty string → default-region path (no region_name kwarg)."""
    import config
    fake_client = mocker.MagicMock()
    # boto3 is imported lazily inside _get_glue, so patch it on the boto3
    # module itself rather than on `config`.
    import boto3
    client_mock = mocker.patch.object(boto3, 'client', return_value=fake_client)

    result = config.get_glue_client('')
    assert result is fake_client
    # Default path uses no region kwarg.
    client_mock.assert_called_once_with('glue')


def test_specific_region_passes_region_name(mocker):
    """Non-empty region → boto3.client('glue', region_name=region)."""
    import config
    fake_client = mocker.MagicMock()
    import boto3
    client_mock = mocker.patch.object(boto3, 'client', return_value=fake_client)

    result = config.get_glue_client('eu-west-1')
    assert result is fake_client
    client_mock.assert_called_once_with('glue', region_name='eu-west-1')


def test_per_region_caching(mocker):
    """Same region twice → boto3.client called only once."""
    import config
    fake_client = mocker.MagicMock()
    import boto3
    client_mock = mocker.patch.object(boto3, 'client', return_value=fake_client)

    a = config.get_glue_client('eu-west-1')
    b = config.get_glue_client('eu-west-1')
    assert a is b
    assert client_mock.call_count == 1


def test_different_regions_get_different_clients(mocker):
    """Each region gets its own boto3 client instance."""
    import config
    import boto3
    # Each call returns a distinct MagicMock so we can verify caching by identity.
    client_mock = mocker.patch.object(boto3, 'client',
                                       side_effect=lambda *a, **kw: mocker.MagicMock())

    a = config.get_glue_client('eu-west-1')
    b = config.get_glue_client('us-west-2')
    assert a is not b
    # Two distinct regions → two boto3.client calls.
    assert client_mock.call_count == 2


def test_default_and_specific_region_are_separate_caches(mocker):
    """The default-region client (None/'') is separate from any explicit
    region — they don't collide in the cache."""
    import config
    import boto3
    client_mock = mocker.patch.object(boto3, 'client',
                                       side_effect=lambda *a, **kw: mocker.MagicMock())

    default = config.get_glue_client('')
    explicit = config.get_glue_client('us-east-1')
    assert default is not explicit
    assert client_mock.call_count == 2


@pytest.mark.parametrize('operation', ['SendTaskSuccess', 'SendTaskFailure'])
def test_the_console_step_functions_client_never_retries_a_closed_token(operation):
    """ADR #135: a closed token is never retried; the client's retry mode must agree with DEAD_TOKEN_CODES."""
    import types
    import botocore.session
    import config
    from botocore.retries.standard import RetryContext, StandardRetryConditions
    from polyris.coordination.wake import DEAD_TOKEN_CODES

    assert config.SFN_CLIENT_CONFIG['retries']['mode'] == 'standard'
    model = botocore.session.get_session().get_service_model('stepfunctions').operation_model(operation)

    def retried(code):
        return StandardRetryConditions().is_retryable(RetryContext(
            attempt_number=1, operation_model=model, parsed_response={'Error': {'Code': code}},
            http_response=types.SimpleNamespace(status_code=400), caught_exception=None))

    assert not [code for code in DEAD_TOKEN_CODES if retried(code)]
    assert retried('ThrottlingException')  # the check can tell a retry from none
