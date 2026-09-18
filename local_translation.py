"""Disposable English-to-Italian CPU translation, without network quotas."""
import os
from pathlib import Path


def model_path():
    return Path(os.getenv('ITALIAN_TRANSLATION_MODEL', '/opt/translation/en_it'))


def available():
    root = model_path()
    return (root / 'model/model.bin').is_file() and (root / 'sentencepiece.model').is_file()


def translate(cues):
    # Imported only in the supervised subtitle child, after Whisper has exited.
    import ctranslate2
    import sentencepiece

    root = model_path()
    tokenizer = sentencepiece.SentencePieceProcessor(model_file=str(root / 'sentencepiece.model'))
    translator = ctranslate2.Translator(str(root / 'model'), device='cpu', compute_type='int8',
                                       inter_threads=1, intra_threads=1)
    translated = {}
    try:
        for _, _, text in cues:
            if text in translated:
                continue
            tokens = tokenizer.encode(text, out_type=str)
            if not tokens or len(tokens) > 256:
                raise ValueError('offline translation input limit')
            result = translator.translate_batch([tokens], beam_size=2, max_input_length=256,
                                                max_decoding_length=256, replace_unknowns=True)[0]
            output = result.hypotheses[0]
            if not output or len(output) >= 256:
                raise ValueError('offline translation incomplete')
            value = tokenizer.decode(output).strip()
            if not value:
                raise ValueError('offline translation empty')
            translated[text] = value
        return [(start, end, translated[text]) for start, end, text in cues]
    finally:
        translator.unload_model()
