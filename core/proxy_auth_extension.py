"""
Cria extensão Chrome temporária pra autenticar proxy automaticamente.
Chrome --proxy-server não suporta user:pass — esta extensão MV2
intercepta 407 e envia credenciais via webRequest.onAuthRequired.
"""
import json
import os
from pathlib import Path
from urllib.parse import urlparse


def create_proxy_auth_extension(proxy_url: str, profile_dir: Path) -> str:
    """
    Cria extensão Chrome pra auth do proxy no profile_dir.
    
    Returns:
        Path da extensão (pra --load-extension=)
    """
    parsed = urlparse(proxy_url)
    host = parsed.hostname or ""
    port = str(parsed.port or 8080)
    username = parsed.username or ""
    password = parsed.password or ""
    scheme = "http"

    ext_dir = Path(str(profile_dir)) / "_proxy_auth_ext"
    ext_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "version": "1.0.0",
        "manifest_version": 2,
        "name": "Proxy Auth",
        "permissions": [
            "proxy",
            "tabs",
            "unlimitedStorage",
            "storage",
            "<all_urls>",
            "webRequest",
            "webRequestBlocking"
        ],
        "background": {
            "scripts": ["background.js"]
        },
        "minimum_chrome_version": "76.0.0"
    }

    background_js = f"""
var config = {{
    mode: "fixed_servers",
    rules: {{
        singleProxy: {{
            scheme: "{scheme}",
            host: "{host}",
            port: parseInt("{port}")
        }},
        bypassList: ["localhost", "127.0.0.1"]
    }}
}};

chrome.proxy.settings.set({{value: config, scope: "regular"}}, function(){{}});

function callbackFn(details) {{
    return {{
        authCredentials: {{
            username: "{username}",
            password: "{password}"
        }}
    }};
}}

chrome.webRequest.onAuthRequired.addListener(
    callbackFn,
    {{urls: ["<all_urls>"]}},
    ['blocking']
);
"""

    (ext_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (ext_dir / "background.js").write_text(background_js, encoding="utf-8")

    return str(ext_dir)
