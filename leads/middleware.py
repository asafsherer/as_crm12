from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse

OPEN_PATHS = ("/login/", "/logout/", "/2fa/", "/static/")


class TwoFactorMiddleware:
    """After password login, require a TOTP code (or force enrolment) before anything else."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and not request.path.startswith(OPEN_PATHS):
            from .perms import prof
            p = prof(user)
            if p.totp_enabled and not request.session.get("otp_ok"):
                return redirect(reverse("twofa_verify"))
            if settings.REQUIRE_2FA and not p.totp_enabled:
                return redirect(reverse("twofa_setup"))
        return self.get_response(request)


class SecurityHeadersMiddleware:
    """CSP, no-store caching for logged-in pages (personal data must not sit in browser caches), and more."""

    CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
           "font-src https://fonts.gstatic.com; img-src 'self' data:; object-src 'none'; base-uri 'self'; "
           "form-action 'self'; frame-ancestors 'none'")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", self.CSP)
        response.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
        response.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        if not request.path.startswith("/static/"):
            response["Cache-Control"] = "no-store, private"
            response["Pragma"] = "no-cache"
        return response
