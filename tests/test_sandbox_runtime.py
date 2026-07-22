"""The runtime seam. Nothing here touches podman — that is the entire point: if this boundary is
not clean, the sandbox becomes untestable in a CI that has no container runtime."""
import pytest

from engine.sandbox.runtime import (ExecResult, FakeRuntime, SandboxUnavailable,
                                    validate_workspace)


def test_exec_result_ok_is_exit_zero_and_not_timed_out():
    assert ExecResult(0, "hi", "").ok is True
    assert ExecResult(1, "", "boom").ok is False
    assert ExecResult(0, "", "", timed_out=True).ok is False


@pytest.mark.parametrize("name", ["default", "research", "ws-1", "a", "a_b-c9"])
def test_valid_workspace_names(name):
    assert validate_workspace(name) == name


@pytest.mark.parametrize("bad", [
    "", "-leading", "_leading", "UPPER", "has space", "has/slash", "has;semi",
    "..", "a" * 33, "--network=host",
])
def test_invalid_workspace_names_are_rejected(bad):
    """A name reaches a podman argv, so anything that could be read as a flag or a path must die
    here rather than downstream."""
    with pytest.raises(ValueError):
        validate_workspace(bad)


def test_fake_records_calls():
    fake = FakeRuntime()
    fake.ensure_workspace("default")
    fake.exec("default", ["python", "-c", "print(1)"])
    assert fake.started == {"default"}
    assert fake.calls == [("default", ["python", "-c", "print(1)"])]


def test_fake_returns_the_canned_result():
    fake = FakeRuntime(result=ExecResult(0, "42\n", ""))
    assert fake.exec("default", ["python", "-c", "print(42)"]).stdout == "42\n"


def test_fake_can_report_unavailable_and_refuses_to_exec():
    fake = FakeRuntime(available_=False)
    assert fake.available() is False
    with pytest.raises(SandboxUnavailable):
        fake.exec("default", ["true"])


def test_fake_validates_workspace_names_like_the_real_one():
    with pytest.raises(ValueError):
        FakeRuntime().ensure_workspace("../escape")
