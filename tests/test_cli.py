from argparse import Namespace

from babel_tv.cli import source_language


def test_channel_language_hint_feeds_whisper():
    assert source_language(Namespace(source_lang=None, language="te")) == "te"


def test_from_overrides_channel_language():
    assert source_language(Namespace(source_lang="ur", language="hi")) == "ur"


def test_no_hint_means_auto_detect():
    assert source_language(Namespace(source_lang=None, language=None)) is None
