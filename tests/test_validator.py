from app.validator import validate_post_html, validate_quiz


def levels(issues, level):
    return [i["text"] for i in issues if i["level"] == level]


GOOD = (
    "<b>Понятие недели: квантизация</b>\n\n"
    "Модель не влезает в видеокарту. Что делать?\n\n"
    '<pre><code class="language-bash">ollama run llama3:8b-q4</code></pre>\n\n'
    "<blockquote expandable>Нюанс: качество падает.</blockquote>\n\n#EML_concept"
)


def test_good_post_has_no_errors():
    assert levels(validate_post_html(GOOD), "error") == []


def test_unsupported_tag_is_error():
    errors = levels(validate_post_html("<p>текст</p>"), "error")
    assert any("<p>" in e for e in errors)


def test_br_is_error():
    assert any("<br>" in e for e in levels(validate_post_html("строка<br>строка"), "error"))


def test_unclosed_and_mismatched_tags():
    assert any("не закрыт" in e for e in levels(validate_post_html("<b>жирный"), "error"))
    assert any("закрыт </i>" in e for e in levels(validate_post_html("<b>жирный</i></b>"), "error"))


def test_raw_less_than_is_error():
    errors = levels(validate_post_html("если x < 10, то всё плохо"), "error")
    assert any("&lt;" in e for e in errors)
    assert levels(validate_post_html("если x &lt; 10 и y &gt; 2"), "error") == []


def test_tag_inside_code_is_error():
    assert any("внутри <code>" in e for e in levels(validate_post_html("<code>a <b>b</b></code>"), "error"))


def test_placeholder_blocks_publication_but_braces_in_code_are_fine():
    assert any("{Мой опыт}" in e for e in levels(validate_post_html("<b>Мой опыт:</b> {Мой опыт}"), "error"))
    code = '<pre><code class="language-python">d = {"a": 1}</code></pre>'
    assert levels(validate_post_html(code), "error") == []


def test_length_limits():
    assert any("лимит Telegram" in e for e in levels(validate_post_html("я" * 4097), "error"))
    # эмодзи занимает две единицы UTF-16: 2049 штук — уже за лимитом
    assert any("лимит Telegram" in e for e in levels(validate_post_html("🔹" * 2049), "error"))
    assert any("рекомендовано до" in w for w in levels(validate_post_html("я" * 300, max_len=200), "warn"))
    assert any("рекомендовано от" in w for w in levels(validate_post_html("я" * 50, min_len=200), "warn"))
    # небольшой выход за рекомендованную длину — не повод для предупреждения
    assert validate_post_html("я" * 2001, max_len=2000) == []
    assert validate_post_html("я" * 1150, min_len=1200) == []


def test_stop_phrase_is_warning_not_error():
    issues = validate_post_html("В современном мире нейросети повсюду.")
    assert levels(issues, "error") == []
    assert any("в современном мире" in w for w in levels(issues, "warn"))


def test_link_and_spoiler_attrs():
    assert levels(validate_post_html('<a href="https://arxiv.org">статья</a>'), "error") == []
    assert any("href" in e for e in levels(validate_post_html("<a>статья</a>"), "error"))
    assert any("onclick" in e for e in levels(validate_post_html('<a href="x" onclick="y">z</a>'), "error"))
    assert levels(validate_post_html('<span class="tg-spoiler">секрет</span>'), "error") == []


QUIZ = {
    "kind": "quiz",
    "question": "Что означает «7B» в названии модели?",
    "options": ["7 миллиардов параметров", "Модель весит 7 ГБ", "7 млрд токенов", "Седьмая версия"],
    "correct_index": 0,
    "explanation": "B = billion параметров.",
}


def test_good_quiz():
    assert validate_quiz(QUIZ) == []


def test_quiz_limits_and_index():
    assert any("вопрос длиннее" in e for e in levels(validate_quiz({**QUIZ, "question": "я" * 301}), "error"))
    assert any("объяснение длиннее" in e for e in levels(validate_quiz({**QUIZ, "explanation": "я" * 201}), "error"))
    long_opt = {**QUIZ, "options": ["а" * 101, "б", "в", "г"]}
    assert any("вариант длиннее" in e for e in levels(validate_quiz(long_opt), "error"))
    assert any("correct_index" in e for e in levels(validate_quiz({**QUIZ, "correct_index": 4}), "error"))
    assert any("correct_index" in e for e in levels(validate_quiz({**QUIZ, "correct_index": None}), "error"))
    assert any("повторяются" in e for e in levels(validate_quiz({**QUIZ, "options": ["а", "А", "б", "в"]}), "error"))
    assert any("explanation" in w for w in levels(validate_quiz({**QUIZ, "explanation": ""}), "warn"))
