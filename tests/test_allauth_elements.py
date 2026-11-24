"""Every django-allauth element is overridden with Unfold components and class lists."""

import re
from pathlib import Path
from typing import Any

import allauth
import pytest
from django.conf import settings
from django.core.cache import cache
from django.template import Engine, Origin, RequestContext, Template
from django.test import Client, RequestFactory
from django.utils.html import escape
from unfold import widgets  # type: ignore[import-untyped]

from accounts.models import User
from core.templatetags.unfold_classes import unfold_classes

pytestmark = pytest.mark.django_db

EMAIL = "ann@example.com"
PASSWORD = "S3cure-passw0rd!"
STOCK_ELEMENTS = Path(allauth.__file__).parent / "templates" / "allauth" / "elements"
PROJECT_ELEMENTS = Path(settings.BASE_DIR) / "templates" / "allauth" / "elements"
UNSUPPORTED = {"provider.html", "provider_list.html"}


@pytest.fixture(autouse=True)
def fresh_rate_limits() -> None:
    """Clear the cache so allauth rate limits do not leak between tests."""
    cache.clear()


def tag_of(body: str, name: str) -> str:
    """Find the opening tag of the form control with the given name.

    Args:
        body: Rendered page.
        name: Value of the name attribute.

    Returns:
        The input tag text.
    """
    start = body.index(f'name="{name}"')
    return body[body.rindex("<input", 0, start) : body.index(">", start) + 1]


def template_names(response: Any) -> set[str]:
    """List the names of the templates rendered for a response.

    Args:
        response: Test client response.

    Returns:
        The template names.
    """
    return {str(t.name) for t in response.templates}


def render_element(source: str) -> str:
    """Render a template snippet that uses allauth elements.

    Args:
        source: Template body, allauth tags already available.

    Returns:
        The rendered HTML.
    """
    origin = Origin(name="probe", template_name="probe.html")
    template = Template("{% load allauth %}" + source, origin, engine=Engine.get_default())
    return template.render(RequestContext(RequestFactory().get("/")))


def test_every_stock_element_has_a_project_override() -> None:
    """A new allauth element fails the build until it is styled in the project."""
    stock = {p.name for p in STOCK_ELEMENTS.glob("*.html")} - UNSUPPORTED
    overridden = {p.name for p in PROJECT_ELEMENTS.glob("*.html")}
    assert sorted(stock - overridden) == []


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("input", widgets.INPUT_CLASSES),
        ("textarea", widgets.TEXTAREA_CLASSES),
        ("select", widgets.SELECT_CLASSES),
        ("checkbox", widgets.CHECKBOX_CLASSES),
        ("radio", widgets.RADIO_CLASSES),
        ("label", widgets.LABEL_CLASSES),
        ("checkbox_label", widgets.CHECKBOX_LABEL_CLASSES),
    ],
)
def test_unfold_classes_tag_returns_the_widget_class_list(kind: str, expected: list[str]) -> None:
    """The tag joins the matching Unfold widget class list.

    Args:
        kind: Control kind passed to the tag.
        expected: Unfold class list for that kind.
    """
    assert unfold_classes(kind) == " ".join(expected)


def test_unfold_classes_tag_rejects_an_unknown_kind() -> None:
    """A typo in a template fails loudly instead of rendering unstyled markup."""
    with pytest.raises(KeyError):
        unfold_classes("nope")


def test_login_inputs_carry_unfold_input_classes(client: Client) -> None:
    """Email and password inputs use Unfold's input classes and the checkbox its own.

    Args:
        client: Django test client fixture.
    """
    body = client.get("/accounts/login/").content.decode()
    for name in ("login", "password"):
        assert " ".join(widgets.INPUT_CLASSES) in tag_of(body, name)
    assert escape(" ".join(widgets.CHECKBOX_CLASSES)) in tag_of(body, "remember")


def test_login_submit_button_uses_the_unfold_button_component(client: Client) -> None:
    """The sign in button is rendered by Unfold's button component.

    Args:
        client: Django test client fixture.
    """
    response = client.get("/accounts/login/")
    assert "unfold/components/button.html" in template_names(response)
    assert 'type="submit"' in response.content.decode()


def test_login_errors_render_through_the_unfold_alert(client: Client) -> None:
    """A failed login shows the form error in Unfold's error box.

    Args:
        client: Django test client fixture.
    """
    response = client.post("/accounts/login/", {"login": EMAIL, "password": "wrong"})
    body = response.content.decode()
    assert "unfold/helpers/messages/error.html" in template_names(response)
    assert "bg-red-100" in body
    assert "not correct" in body


def test_field_errors_render_through_the_unfold_field_error_markup(client: Client) -> None:
    """A field error uses Unfold's form error template.

    Args:
        client: Django test client fixture.
    """
    response = client.post(
        "/accounts/signup/",
        {"email": EMAIL, "password1": PASSWORD, "password2": "different"},
    )
    assert "unfold/helpers/form_errors.html" in template_names(response)
    assert "text-red-600" in response.content.decode()


def test_password_is_never_echoed_back_into_the_form(client: Client) -> None:
    """A rejected password is not rendered as a value attribute.

    Args:
        client: Django test client fixture.
    """
    body = client.post("/accounts/login/", {"login": EMAIL, "password": "leak-me"}).content.decode()
    assert "leak-me" not in body
    assert f'value="{EMAIL}"' in tag_of(body, "login")


def test_email_management_radios_and_badges_are_unfold_styled(client: Client) -> None:
    """The email list renders radios with Unfold classes and badges with its badge helper.

    Args:
        client: Django test client fixture.
    """
    from allauth.account.models import EmailAddress

    user = User.objects.create_user("ann", EMAIL, PASSWORD)
    EmailAddress.objects.create(user=user, email=EMAIL, verified=True, primary=True)
    client.force_login(user)
    response = client.get("/accounts/email/")
    body = response.content.decode()
    assert escape(" ".join(widgets.RADIO_CLASSES)) in tag_of(body, "email")
    assert "unfold/helpers/badge.html" in template_names(response)
    assert "Primary" in body


DETAILS = "{% element details %}{% slot summary %}S{% endslot %}{% slot body %}B{% endslot %}"
PANEL = "{% element panel %}{% slot title %}T{% endslot %}{% slot body %}B{% endslot %}"
CELL = '{% element td align="right" %}D{% endelement %}'
HEAD = "{% element thead %}{% element tr %}{% element th %}H{% endelement %}"
TABLE = (
    "{% element table %}"
    + HEAD
    + "{% endelement %}{% endelement %}"
    + "{% element tbody %}{% element tr %}"
    + CELL
    + "{% endelement %}{% endelement %}{% endelement %}"
)


@pytest.mark.parametrize(
    ("source", "marker"),
    [
        (
            '{% element alert tags="success" %}{% slot message %}Hi{% endslot %}{% endelement %}',
            "bg-green-100",
        ),
        ('{% element badge tags="danger" %}Gone{% endelement %}', "bg-red-100"),
        ("{% element button_group vertical=True %}x{% endelement %}", "flex-col"),
        (DETAILS + "{% endelement %}", "cursor-pointer"),
        ("{% element h1 %}Title{% endelement %}", "tracking-tight"),
        ("{% element h2 %}Title{% endelement %}", "font-semibold"),
        ("{% element hr %}{% endelement %}", "h-px"),
        ('{% element img src="/x.png" alt="x" %}{% endelement %}', "rounded-default"),
        ("{% element p %}Text{% endelement %}", "leading-relaxed"),
        (PANEL + "{% slot actions %}A{% endslot %}{% endelement %}", "shadow-xs"),
        (TABLE, "border-separate"),
    ],
)
def test_every_remaining_element_renders_unfold_markup(source: str, marker: str) -> None:
    """Each element renders without error and carries Unfold's classes.

    Args:
        source: Template using the element.
        marker: Class that only Unfold styling produces.
    """
    assert marker in render_element(source)


def test_badge_renders_its_text_inside_the_span() -> None:
    """The badge span holds the slot text, not just the Unfold classes."""
    html = render_element('{% element badge tags="success" %}Verified{% endelement %}')
    assert re.search(r"<span[^>]*>\s*Verified\s*</span>", html)


def test_email_page_badges_show_their_labels(client: Client) -> None:
    """The Verified and Primary badges on the email page are not empty boxes.

    Args:
        client: Django test client fixture.
    """
    from allauth.account.models import EmailAddress

    user = User.objects.create_user("ann", EMAIL, PASSWORD)
    EmailAddress.objects.create(user=user, email=EMAIL, verified=True, primary=True)
    client.force_login(user)
    body = client.get("/accounts/email/").content.decode()
    for label in ("Verified", "Primary"):
        assert re.search(rf"<span[^>]*>\s*{label}\s*</span>", body)


def test_links_inside_a_paragraph_element_are_styled() -> None:
    """An inline link sits inside a wrapper carrying Unfold's link class."""
    html = render_element('{% element p %}See <a href="/x/">this</a>{% endelement %}')
    wrapper = html[: html.index('<a href="/x/"')]
    assert unfold_classes("prose_link") in wrapper
    assert "prose-a:text-link" in unfold_classes("prose_link")


def test_signup_and_login_pages_wrap_their_inline_links(client: Client) -> None:
    """The sign in link on signup and the forgot password link on login are styled.

    Args:
        client: Django test client fixture.
    """
    link_class = unfold_classes("prose_link")
    signup = client.get("/accounts/signup/").content.decode()
    assert link_class in signup[: signup.index('<a href="/accounts/login/"')]
    login = client.get("/accounts/login/").content.decode()
    forgot = login.index('<a href="/accounts/password/reset/"')
    assert link_class in login[login.rindex("</label>", 0, forgot) : forgot]
