from smart_overlay.math_render import render_formula, replace_math


def marked(text: str) -> str:
    return replace_math(text, lambda latex, display, source: f"<{'D' if display else 'I'}:{latex}>")


def test_finds_all_delimiters():
    assert marked(r"дробь $\frac{M}{L}$ тут") == r"дробь <I:\frac{M}{L}> тут"
    assert marked(r"и \(x_0\) тоже") == "и <I:x_0> тоже"
    assert marked("$$\na^2\n$$") == "<D:a^2>"
    assert marked(r"\[ b^2 \]") == "<D:b^2>"
    assert marked(r"**$M$ піде** і $y - y_0$") == "**<I:M> піде** і <I:y - y_0>"


def test_money_and_code_stay_text():
    for text in ("от $5 до $10", "цена $5", r"экранировано \$x\$", "код `a = $b$`", "```\n$x$\n```", "```\n$x$"):
        assert marked(text) == text


def test_unfinished_formula_waits_for_the_closing_dollar():
    assert marked(r"пишется $\frac{M}") == r"пишется $\frac{M}"
    assert marked("блок $$a^2") == "блок $$a^2"


def test_source_keeps_delimiters():
    sources = []
    replace_math(r"$a$ и $$b$$", lambda latex, display, source: sources.append(source) or "")
    assert sources == ["$a$", "$$b$$"]


def render(latex: str, display: bool = False, ratio: float = 1.0):
    return render_formula(latex, display, latex, 16.0, "#ffffff", 4.0, ratio)


def test_renders_formula_at_device_resolution():
    formula = render(r"\frac{M}{L}", ratio=2.0)
    assert formula.width > 5 and formula.height > 10
    assert formula.image.width() == round(formula.width * 2) and formula.image.devicePixelRatio() == 2.0
    painted = {formula.image.pixelColor(x, y).alpha()
               for x in range(formula.image.width()) for y in range(formula.image.height())}
    assert max(painted) > 200


def test_inline_formula_is_padded_to_the_text_baseline():
    # Дробь уходит под базовую линию, поэтому строчная картинка выше такой же отдельной: снизу или сверху поле
    assert render(r"\frac{M}{L}^{2}").height != render(r"\frac{M}{L}^{2}", display=True).height


def test_matrices_cases_and_cyrillic():
    for latex in (r"\begin{pmatrix} a & b \\ c & d \end{pmatrix}", r"\begin{cases} x = 1 \\ y = 2 \end{cases}",
                  r"v = \frac{s}{t} \text{ км/ч}"):
        assert render(latex, display=True) is not None


def test_broken_latex_is_not_an_error():
    assert render(r"\frac{x") is None
