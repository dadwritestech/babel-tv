from babel_tv.translate import Translator, clean


def test_messages_mark_context_and_new_line():
    t = Translator("te", api_key="")
    t.context.extend(["एक", "दो"])
    msgs = t.build_messages("तीन", "hi")
    assert "Telugu" in msgs[0]["content"] and "Hindi" in msgs[0]["content"]
    assert msgs[1]["content"] == "PREVIOUS:\nएक\nदो\n\nNEW: तीन"


def test_same_language_passthrough():
    t = Translator("hi", api_key="")
    assert t.translate("नमस्ते", "hi") == "नमस्ते"


def test_retry_without_context_when_model_repeats_itself():
    t = Translator("te", api_key="")
    calls = []
    replies = iter(["మొదటి", "మొదటి", "రెండవ"])
    def fake_post(body):
        calls.append(body["messages"][1]["content"])
        return next(replies)
    t._post = fake_post
    assert t.translate("पहला", "hi") == "మొదటి"
    assert t.translate("दूसरा", "hi") == "రెండవ"
    assert calls[2] == "NEW: दूसरा"  # retried without the context block


def test_clean_strips_labels_and_quotes():
    assert clean('NEW: "నమస్కారం"') == "నమస్కారం"
    assert clean("“line one\nline two”") == "line one line two"
