"""极验后端的离线契约与凭据边界测试。"""

import importlib.util
import json
import sys
import traceback
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src/nonebot_plugin_group_guard/web.py"


def load_module():
    if not MODULE_PATH.exists():
        return None
    spec = importlib.util.spec_from_file_location("_guard_web_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


WEB = load_module()
KEY = "unit-test-key-not-a-real-secret"
BASE = "https://verify.example.test"


class BackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.assertIsNotNone(WEB, "极验后端模块尚未实现")
        self.clients = []
        self.backends = []

    async def asyncTearDown(self):
        for backend in getattr(self, "backends", []):
            await backend.close()
        for client in getattr(self, "clients", []):
            await client.aclose()

    def make_backend(self, handler, *, base=BASE, follow_redirects=False):
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=follow_redirects,
            timeout=None,
        )
        self.clients.append(client)
        backend = WEB.VerificationBackend(base, KEY, client=client)
        self.backends.append(backend)
        return backend, client

    async def test_create_posts_string_ids_bearer_and_preserves_query(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={
                "code": 0,
                "data": {"url": BASE + "/verify/a%2Fb?ticket=a%2Bb&next=%2F&ticket=z"},
            })

        backend, _ = self.make_backend(handler)
        result = await backend.create(10001, "00020002")
        self.assertEqual(result, BASE + "/verify/a%2Fb?ticket=a%2Bb&next=%2F&ticket=z")
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual((request.method, str(request.url)), ("POST", BASE + "/api/verify/create"))
        self.assertEqual(json.loads(request.content), {"group_id": "10001", "user_id": "00020002"})
        self.assertEqual(request.headers["Authorization"], "Bearer " + KEY)
        self.assertEqual(request.headers["Content-Type"], "application/json")
        self.assertTrue(all(value is not None and value > 0 for value in request.extensions["timeout"].values()))

    async def test_configured_base_path_is_preserved(self):
        requests = []

        def handler(request):
            requests.append(str(request.url))
            return httpx.Response(200, json={"code": 0, "data": {"url": "/verify?id=42"}})

        backend, _ = self.make_backend(handler, base=BASE + "/gateway/")
        self.assertEqual(await backend.create(1, 2), BASE + "/verify?id=42")
        self.assertEqual(requests, [BASE + "/gateway/api/verify/create"])

    async def test_same_origin_default_port_is_allowed(self):
        url = "https://VERIFY.example.test:443/verify?q=a%2Bb"
        backend, _ = self.make_backend(lambda _: httpx.Response(200, json={"code": 0, "data": {"url": url}}))
        self.assertEqual(await backend.create(1, 2), url)

    async def test_idna_distinct_origins_are_not_treated_as_aliases(self):
        for base, url in [
            ("https://fass.example.test", "https://faß.example.test/verify"),
            ("https://faß.example.test", "https://fass.example.test/verify"),
        ]:
            with self.subTest(base=base, url=url):
                backend, _ = self.make_backend(lambda _, url=url: httpx.Response(200, json={"code": 0, "data": {"url": url}}), base=base)
                with self.assertRaises(WEB.BackendError):
                    await backend.create(1, 2)

    async def test_unicode_origin_accepts_its_actual_punycode_form(self):
        url = "https://xn--fa-hia.example.test/verify?ticket=a%2Bb"
        backend, _ = self.make_backend(lambda _: httpx.Response(200, json={"code": 0, "data": {"url": url}}), base="https://faß.example.test")
        self.assertEqual(await backend.create(1, 2), url)

    async def test_untrusted_or_malformed_create_urls_raise(self):
        urls = [
            "https://evil.example.test/verify", "http://verify.example.test/verify",
            "https://verify.example.test:444/verify", "https://verify.example.test.evil.test/verify",
            "https://verify.example.test@evil.test/verify", "https://user@verify.example.test/verify",
            "//evil.example.test/verify", "javascript:alert(1)", "https://verify.example.test\\@evil.test/",
            " https://verify.example.test/verify", "https://verify.example.test/verify\n",
            "https://verify.example.test:bad/verify", "https://verify.example.test./verify",
            "https://verify.example.test/verify#fragment", "", None, 42,
        ]
        for url in urls:
            with self.subTest(url=url):
                backend, _ = self.make_backend(lambda _, url=url: httpx.Response(200, json={"code": 0, "data": {"url": url}}))
                with self.assertRaises(WEB.BackendError):
                    await backend.create(1, 2)

    async def test_relative_create_urls_preserve_query_and_reject_controls(self):
        url = "/verify/a%2Fb?ticket=a%2Bb&ticket=000042"
        backend, _ = self.make_backend(lambda _: httpx.Response(200, json={"code": 0, "data": {"url": url}}))
        self.assertEqual(await backend.create(1, 2), BASE + url)
        for bad in ["/verify\n", "/verify?token=a\tb", "/verify\r?ticket=x", "/verify bad"]:
            with self.subTest(url=bad):
                backend, _ = self.make_backend(lambda _, bad=bad: httpx.Response(200, json={"code": 0, "data": {"url": bad}}))
                with self.assertRaises(WEB.BackendError):
                    await backend.create(1, 2)

    async def test_relative_create_url_keeps_literal_path_and_empty_query(self):
        for path in ["/verify/../ticket?ticket=a%2Bb", "/verify?"]:
            with self.subTest(path=path):
                backend, _ = self.make_backend(lambda _, path=path: httpx.Response(200, json={"code": 0, "data": {"url": path}}))
                self.assertEqual(await backend.create(1, 2), BASE + path)

    async def test_injected_client_auth_cannot_replace_api_bearer(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"code": 0, "passed": True})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=httpx.BasicAuth("test", "unused"))
        self.clients.append(client)
        backend = WEB.VerificationBackend(BASE, KEY, client=client)
        self.backends.append(backend)
        self.assertIs(await backend.check(1, 2, "123456"), True)
        self.assertEqual(requests[0].headers["Authorization"], "Bearer " + KEY)

    async def test_deep_invalid_json_is_still_backend_error(self):
        malformed = "[" * 20000 + "]" * 20000
        backend, _ = self.make_backend(lambda _: httpx.Response(200, text=malformed))
        with self.assertRaises(WEB.BackendError):
            await backend.check(1, 2, "123456")

    async def test_bad_base_urls_fail_without_a_request(self):
        for base in ["", "ftp://verify.example.test", "//verify.example.test", "https://user:password@verify.example.test", BASE + "?secret=1", BASE + "#x", BASE + "\\x", BASE + ":wrong", BASE + "\n"]:
            with self.subTest(base=base):
                with self.assertRaises(WEB.BackendError):
                    WEB.VerificationBackend(base, KEY)

    async def test_missing_key_fails_without_network(self):
        def handler(_):
            self.fail("缺少密钥时不应发请求")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.clients.append(client)
        with self.assertRaises(WEB.BackendError):
            backend = WEB.VerificationBackend(BASE, "", client=client)
            self.backends.append(backend)
            await backend.create(1, 2)

    async def test_check_distinguishes_true_and_false_without_changing_code(self):
        for passed in [True, False]:
            with self.subTest(passed=passed):
                requests = []

                def handler(request):
                    requests.append(request)
                    return httpx.Response(200, json={"code": 0, "passed": passed})

                backend, _ = self.make_backend(handler)
                self.assertIs(await backend.check(1, 2, "00aB09"), passed)
                self.assertEqual(str(requests[0].url), BASE + "/api/verify/check")
                self.assertEqual(json.loads(requests[0].content), {"group_id": "1", "user_id": "2", "code": "00aB09"})
                self.assertEqual(requests[0].headers["Authorization"], "Bearer " + KEY)

    async def test_check_format_or_server_errors_do_not_become_wrong_answers(self):
        payloads = [
            {"code": 1, "passed": False}, {"code": 500, "msg": KEY}, {}, [], None,
            {"code": "0", "passed": False}, {"code": False, "passed": False},
            {"code": 0}, {"code": 0, "passed": "false"}, {"code": 0, "passed": 1},
            {"code": 0, "data": {"passed": True}},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                backend, _ = self.make_backend(lambda _, payload=payload: httpx.Response(200, json=payload))
                with self.assertRaises(WEB.BackendError):
                    await backend.check(1, 2, "123456")

    async def test_upstream_http400_invalid_code_is_wrong_answer_not_outage(self):
        messages = [
            "验证失败：验证码已使用", "验证失败：验证码已过期",
            "验证失败：验证码未完成验证", "验证失败：验证码不存在或已失效",
            "验证失败：用户ID不匹配",
        ]
        for message in messages:
            with self.subTest(message=message):
                backend, _ = self.make_backend(lambda _, message=message: httpx.Response(
                    400, json={"code": 400, "passed": False, "msg": message}))
                self.assertIs(await backend.check(1, 2, "123456"), False)

    async def test_http400_bad_parameters_remain_backend_error(self):
        backend, _ = self.make_backend(lambda _: httpx.Response(
            400, json={"code": 400, "passed": False, "msg": "参数错误"}))
        with self.assertRaises(WEB.BackendError):
            await backend.check(1, 2, "123456")

    async def test_create_rejects_invalid_response_shapes(self):
        for payload in [{"code": 1, "data": {"url": BASE}}, {"code": 0}, {"code": 0, "data": None}, {"code": 0, "data": []}, {"code": 0, "data": {}}]:
            with self.subTest(payload=payload):
                backend, _ = self.make_backend(lambda _, payload=payload: httpx.Response(200, json=payload))
                with self.assertRaises(WEB.BackendError):
                    await backend.create(1, 2)

    async def test_http_and_invalid_json_are_backend_errors(self):
        responses = [
            httpx.Response(401, text=KEY), httpx.Response(429), httpx.Response(503, text=KEY),
            httpx.Response(200, text=KEY), httpx.Response(204),
        ]
        for response in responses:
            for operation in ["create", "check"]:
                with self.subTest(status=response.status_code, operation=operation):
                    backend, _ = self.make_backend(lambda _, response=response: response)
                    with self.assertRaises(WEB.BackendError) as caught:
                        if operation == "create":
                            await backend.create(1, 2)
                        else:
                            await backend.check(1, 2, "123456")
                    self.assertNotIn(KEY, str(caught.exception))

    async def test_transport_exception_does_not_disclose_secret_in_traceback(self):
        def handler(request):
            raise httpx.ConnectError("upstream echoed " + KEY, request=request)

        backend, _ = self.make_backend(handler)
        for operation in ["create", "check"]:
            try:
                if operation == "create":
                    await backend.create(1, 2)
                else:
                    await backend.check(1, 2, "123456")
            except WEB.BackendError:
                self.assertNotIn(KEY, traceback.format_exc())
            else:
                self.fail("网络异常必须上抛 BackendError")
        self.assertNotIn(KEY, repr(backend))

    async def test_redirects_are_never_followed_even_on_injected_client(self):
        for location in ["https://evil.example.test/steal", BASE + "/different"]:
            for operation in ["create", "check"]:
                with self.subTest(location=location, operation=operation):
                    requests = []

                    def handler(request):
                        requests.append(request)
                        return httpx.Response(307, headers={"Location": location})

                    backend, _ = self.make_backend(handler, follow_redirects=True)
                    with self.assertRaises(WEB.BackendError):
                        if operation == "create":
                            await backend.create(1, 2)
                        else:
                            await backend.check(1, 2, "123456")
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(requests[0].url.host, "verify.example.test")

    async def test_close_does_not_close_injected_client(self):
        backend, client = self.make_backend(lambda _: httpx.Response(200, json={"code": 0, "passed": True}))
        await backend.close()
        await backend.close()
        self.assertFalse(client.is_closed)
        self.assertIs(await backend.check(1, 2, "123456"), True)

    async def test_close_releases_owned_client(self):
        owned = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
        self.clients.append(owned)
        with patch.object(WEB.httpx, "AsyncClient", return_value=owned):
            backend = WEB.VerificationBackend(BASE, KEY)
        self.backends.append(backend)
        self.assertFalse(owned.is_closed)
        await backend.close()
        await backend.close()
        self.assertTrue(owned.is_closed)


if __name__ == "__main__":
    unittest.main()
