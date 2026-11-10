from horo_dcb.codex import choose_model


def model(model_id: str, efforts: tuple[str, ...] = ("low", "medium", "high"), default_effort: str = "medium", is_default: bool = False) -> dict:
    return {
        "id": model_id,
        "isDefault": is_default,
        "defaultReasoningEffort": default_effort,
        "supportedReasoningEfforts": [{"reasoningEffort": effort} for effort in efforts],
    }


MODELS = [model("first"), model("server-default", is_default=True, default_effort="high"), model("fast", efforts=("low",), default_effort="low")]


def test_no_models_keeps_the_stored_effort():
    assert choose_model([], "anything", "high") == (None, "high")


def test_stored_model_and_supported_effort_are_used():
    chosen, effort = choose_model(MODELS, "first", "low")
    assert chosen["id"] == "first"
    assert effort == "low"


def test_unknown_model_falls_back_to_the_server_default():
    chosen, effort = choose_model(MODELS, "retired", None)
    assert chosen["id"] == "server-default"
    assert effort == "high"


def test_without_a_server_default_the_first_model_is_used():
    chosen, _ = choose_model([model("a"), model("b")], None, None)
    assert chosen["id"] == "a"


def test_unsupported_effort_falls_back_to_the_model_default():
    chosen, effort = choose_model(MODELS, "fast", "high")
    assert chosen["id"] == "fast"
    assert effort == "low"
