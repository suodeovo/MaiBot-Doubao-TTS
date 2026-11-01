"""离线测试：不读取用户配置，不访问真实合成服务，不发送平台消息。"""

from __future__ import annotations

import base64
import json
import socket
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import legacy_speaker_guard as speaker_checks
import legacy_volc_tts as legacy

module = legacy.load_module()


class OfflineCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        network = patch.object(socket.socket, "connect", side_effect=AssertionError("Tests must stay offline"))
        network.start()
        self.addCleanup(network.stop)


class ExistingBehaviourTests(OfflineCase):
    async def test_emotion_prefix(self):
        await legacy.case_emotion(module)

    async def test_command_patterns(self):
        await legacy.case_command_patterns(module)

    async def test_synthesis_and_voice_send(self):
        await legacy.case_speak_success(module)

    async def test_length_and_command_cooldown(self):
        await legacy.case_length_and_cooldown(module)

    async def test_service_error_message(self):
        await legacy.case_service_error(module)

    async def test_completed_synthesis_cache(self):
        await legacy.case_cache(module)

    async def test_empty_command_usage(self):
        await legacy.case_debug_command(module)

    async def test_speaker_guard_matrix(self):
        await speaker_checks.main()


class ApiTests(OfflineCase):
    def make(self, **settings):
        plugin = legacy.make_plugin(module)
        values = plugin.get_plugin_config_data()
        for section, fields in settings.items():
            values.setdefault(section, {}).update(fields)
        plugin.set_plugin_config(values)
        plugin._synthesize = AsyncMock(return_value=legacy.FAKE_MP3)
        return plugin

    async def test_disabled_api_never_synthesizes(self):
        plugin = self.make(plugin={"enabled": False})
        result = await plugin.api_synthesize("hello")
        self.assertFalse(result["success"])
        plugin._synthesize.assert_not_awaited()

    async def test_overlong_api_never_synthesizes(self):
        plugin = self.make(plugin={"max_text_length": 3})
        result = await plugin.api_synthesize("hello")
        self.assertFalse(result["success"])
        self.assertIn("过长", result["error"])
        plugin._synthesize.assert_not_awaited()

    async def test_empty_api_never_synthesizes(self):
        plugin = self.make()
        result = await plugin.api_synthesize("[CQ:at,qq=123]")
        self.assertFalse(result["success"])
        plugin._synthesize.assert_not_awaited()

    async def test_api_returns_bytes_without_sending(self):
        plugin = self.make()
        result = await plugin.api_synthesize("hello", speaker="小何")
        self.assertTrue(result["success"])
        self.assertEqual(base64.b64decode(result["audio_base64"]), legacy.FAKE_MP3)
        self.assertEqual(result["mime_type"], "audio/mpeg")
        self.assertEqual(result["bytes"], len(legacy.FAKE_MP3))
        plugin._synthesize.assert_awaited_once_with("hello", "小何")
        self.assertEqual(plugin.ctx.send.customs, [])

    async def test_pcm_api_metadata(self):
        plugin = self.make(tts={"audio_format": "pcm"})
        result = await plugin.api_synthesize("hello")
        self.assertTrue(result["success"])
        self.assertEqual(result["mime_type"], "audio/octet-stream")

    async def test_api_errors_are_reported(self):
        plugin = self.make()
        plugin._synthesize.side_effect = module.VolcTtsError("45000030", "not granted")
        result = await plugin.api_synthesize("hello")
        self.assertFalse(result["success"])
        self.assertIn("45000030", result["error"])


class HttpProtocolTests(OfflineCase):
    def make(self):
        plugin = legacy.make_plugin(module)
        self.addAsyncCleanup(plugin.on_unload)
        return plugin

    async def test_api_key_precedes_old_auth(self):
        plugin = self.make()
        config = plugin.get_plugin_config_data()
        config["credential"].update(api_key="synthetic-key", app_id="1", access_key="unused")
        plugin.set_plugin_config(config)
        headers = plugin._build_headers()
        self.assertEqual(headers["X-Api-Key"], "synthetic-key")
        self.assertNotIn("X-Api-Access-Key", headers)
        self.assertNotIn("X-Api-App-Id", headers)

    async def test_missing_auth_is_rejected(self):
        plugin = self.make()
        config = plugin.get_plugin_config_data()
        config["credential"].update(api_key="", app_id="", access_key="")
        plugin.set_plugin_config(config)
        with self.assertRaises(module.VolcTtsError):
            plugin._build_headers()

    async def test_request_and_stream_assembly(self):
        plugin = self.make()
        config = plugin.get_plugin_config_data()
        config["tts"].update(speech_rate=10)
        plugin.set_plugin_config(config)
        requests = []

        def respond(request):
            requests.append(request)
            lines = [
                {"code": 0, "data": base64.b64encode(b"audio-part-1").decode()},
                {"code": 20000000, "data": base64.b64encode(b"audio-part-2").decode()},
            ]
            return httpx.Response(200, text="\n".join(json.dumps(item) for item in lines))

        plugin._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        audio = await plugin._request_once("[开心] hello", module.DEFAULT_SPEAKER)
        self.assertEqual(audio, b"audio-part-1audio-part-2")
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["req_params"]["text"], "hello")
        additions = json.loads(payload["req_params"]["additions"])
        self.assertEqual(additions["speech_rate"], 10)
        self.assertEqual(additions["context_texts"], ["用开心的语气说"])

    async def test_http_business_error_does_not_retry(self):
        plugin = self.make()
        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(403, json={"header": {"code": 45000030, "message": "not granted"}})

        plugin._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with self.assertRaises(module.VolcTtsError) as caught:
            await plugin._synthesize_with_retry("hello", module.DEFAULT_SPEAKER)
        self.assertEqual(caught.exception.code, "45000030")
        self.assertEqual(len(calls), 1)

    async def test_empty_stream_is_rejected(self):
        plugin = self.make()
        plugin._client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text='{"code":20000000}')))
        with self.assertRaises(module.VolcTtsError):
            await plugin._request_once("hello", module.DEFAULT_SPEAKER)


class LifecycleTests(OfflineCase):
    async def test_load_reload_and_unload_without_requests(self):
        plugin = legacy.make_plugin(module)
        self.addAsyncCleanup(plugin.on_unload)
        await plugin.on_load()
        old_client = plugin._client
        plugin._cache[("hello", module.DEFAULT_SPEAKER, 0, 0)] = b"audio"
        await plugin.on_config_update("self", {}, "1.0.0")
        self.assertTrue(old_client.is_closed)
        self.assertEqual(len(plugin._cache), 0)
        await plugin.on_unload()
        self.assertIsNone(plugin._client)


if __name__ == "__main__":
    unittest.main()
