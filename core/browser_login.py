"""
Login via browser real (Chrome) com resolução automática de challenge.

Fluxo:
1. Abre Chrome headless com Selenium
2. Faz login no Instagram via web
3. Resolve challenge (código do email via tempmail)
4. Extrai cookies válidos
5. Usa cookies pra criar sessão mobile via instagrapi (relogin com sessionid)
6. Salva sessão pro worker usar

Isso contorna o bloks challenge que a API mobile recebe.
"""
import json
import time
import re
import os
from pathlib import Path
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


def browser_login(
    username: str,
    password: str,
    email: str = None,
    proxy: str = None,
    headless: bool = False,
    timeout: int = 120,
) -> Optional[dict]:
    """
    Faz login no Instagram via Chrome real e retorna cookies.

    Se challenge for pedido e email fornecido, busca código no tempmail.

    Returns:
        dict com cookies do Instagram ou None se falhou.
    """
    print(f"[browser-login] iniciando pra @{username}")

    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1280,800")
    options.add_argument("--lang=pt-BR")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    # Proxy
    if proxy:
        # Selenium não suporta proxy com auth nativamente pra HTTPS
        # Usa extensão ou ignora auth aqui (DataImpulse pode usar IP whitelist)
        proxy_clean = proxy
        if "@" in proxy:
            # Remove auth pra --proxy-server (funciona com IP whitelisted)
            parts = proxy.split("@")
            proxy_clean = "http://" + parts[-1]
        options.add_argument(f"--proxy-server={proxy_clean}")

    # User data dir isolado por conta
    profile_dir = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent.parent)))
    profile_dir = profile_dir / "browser_profiles" / username
    profile_dir.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={profile_dir}")

    driver = None
    try:
        driver = webdriver.Chrome(options=options)
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        })

        # 1. Navega pra página de login
        print(f"[browser-login] abrindo Instagram...")
        driver.get("https://www.instagram.com/accounts/login/")
        time.sleep(4)

        # Aceita cookies se aparecer
        try:
            cookie_btn = driver.find_elements(By.XPATH, "//button[contains(text(), 'Allow') or contains(text(), 'Permitir') or contains(text(), 'Accept')]")
            if cookie_btn:
                cookie_btn[0].click()
                time.sleep(1)
        except Exception:
            pass

        # 2. Preenche login
        print(f"[browser-login] preenchendo credenciais...")
        try:
            user_input = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input[name='username'], input[name='email']"))
            )
            user_input.clear()
            user_input.send_keys(username)
            time.sleep(0.5)

            pass_input = driver.find_element(By.CSS_SELECTOR, "input[name='password'], input[name='pass']")
            pass_input.clear()
            pass_input.send_keys(password)
            time.sleep(0.5)

            # Clica login
            login_btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
            login_btn.click()
            print(f"[browser-login] login enviado, aguardando...")
            time.sleep(8)
        except Exception as e:
            print(f"[browser-login] erro preenchendo: {e}")
            return None

        # 3. Verifica se precisa de challenge
        current_url = driver.current_url
        page_source = driver.page_source.lower()

        if "challenge" in current_url or "verify" in current_url or "security_code" in page_source:
            print(f"[browser-login] challenge detectado!")

            # Tenta clicar "Enviar código por email" se tiver opção
            try:
                email_options = driver.find_elements(By.XPATH,
                    "//button[contains(text(), 'email') or contains(text(), 'Email') or contains(text(), 'e-mail')]"
                )
                if email_options:
                    email_options[0].click()
                    time.sleep(3)
            except Exception:
                pass

            # Busca código no tempmail
            if email:
                print(f"[browser-login] buscando código em {email}...")
                try:
                    from core.tempmail import fetch_instagram_code
                    code = fetch_instagram_code(email, timeout=timeout, poll_interval=4)
                    if code:
                        print(f"[browser-login] código encontrado: {code}")
                        # Preenche código
                        try:
                            code_input = WebDriverWait(driver, 10).until(
                                EC.presence_of_element_located((By.CSS_SELECTOR,
                                    "input[name='security_code'], input[name='verificationCode'], input[type='text'], input[type='number']"))
                            )
                            code_input.clear()
                            code_input.send_keys(code)
                            time.sleep(1)

                            # Clica confirmar
                            confirm_btns = driver.find_elements(By.CSS_SELECTOR, "button[type='button'], button[type='submit']")
                            for btn in confirm_btns:
                                txt = btn.text.lower()
                                if any(w in txt for w in ["confirm", "enviar", "submit", "next", "continuar", "verificar"]):
                                    btn.click()
                                    break
                            else:
                                if confirm_btns:
                                    confirm_btns[-1].click()

                            print(f"[browser-login] código enviado!")
                            time.sleep(8)
                        except Exception as e:
                            print(f"[browser-login] erro preenchendo código: {e}")
                    else:
                        print(f"[browser-login] código não chegou!")
                        return None
                except Exception as e:
                    print(f"[browser-login] erro buscando código: {e}")
                    return None
            else:
                print(f"[browser-login] sem email cadastrado — não pode resolver challenge")
                return None

        # 4. Verifica se logou
        time.sleep(3)
        cookies = driver.get_cookies()
        ig_cookies = {}
        for c in cookies:
            if "instagram.com" in c.get("domain", ""):
                ig_cookies[c["name"]] = c["value"]

        sessionid = ig_cookies.get("sessionid")
        ds_user_id = ig_cookies.get("ds_user_id")

        if sessionid and ds_user_id:
            print(f"[browser-login] LOGADO! sessionid=...{sessionid[-12:]}")
            return ig_cookies
        else:
            print(f"[browser-login] login falhou — cookies: {list(ig_cookies.keys())}")
            # Verifica se ainda está em página de challenge
            current = driver.current_url
            print(f"[browser-login] URL atual: {current}")
            return None

    except Exception as e:
        print(f"[browser-login] erro: {e}")
        return None
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def browser_login_and_create_session(
    username: str,
    password: str,
    email: str = None,
    proxy: str = None,
    workspace_slug: str = "default",
    headless: bool = False,
) -> bool:
    """
    Faz login via browser e cria sessão válida pro instagrapi.

    Returns:
        True se sessão criada com sucesso.
    """
    cookies = browser_login(username, password, email, proxy, headless)
    if not cookies:
        return False

    sessionid = cookies.get("sessionid")
    ds_user_id = cookies.get("ds_user_id")
    if not sessionid or not ds_user_id:
        print(f"[browser-login] sem sessionid/ds_user_id")
        return False

    # Monta session.json no formato instagrapi
    import secrets
    import uuid as _uuid

    session_data = {
        "uuids": {
            "phone_id": str(_uuid.uuid4()),
            "uuid": str(_uuid.uuid4()),
            "client_session_id": str(_uuid.uuid4()),
            "advertising_id": str(_uuid.uuid4()),
            "android_device_id": "android-" + secrets.token_hex(8),
            "request_id": str(_uuid.uuid4()),
            "tray_session_id": str(_uuid.uuid4()),
        },
        "mid": cookies.get("mid", ""),
        "ig_u_rur": cookies.get("rur"),
        "ig_www_claim": "",
        "authorization_data": {
            "ds_user_id": ds_user_id,
            "sessionid": sessionid,
        },
        "cookies": cookies,
        "last_login": time.time(),
        "device_settings": {
            "app_version": "428.0.0.47.67",
            "android_version": 34,
            "android_release": "14",
            "dpi": "480dpi",
            "resolution": "1344x2992",
            "manufacturer": "Google/google",
            "device": "husky",
            "model": "Pixel 8 Pro",
            "cpu": "husky",
            "version_code": "961145276",
        },
        "user_agent": "Instagram 428.0.0.47.67 Android (34/14; 480dpi; 1344x2992; Google/google; Pixel 8 Pro; husky; husky; pt_BR; 961145276)",
        "country": "BR",
        "country_code": 55,
        "locale": "pt_BR",
        "timezone_offset": -10800,
        "manually_saved": True,
        "from_chrome": True,
        "from_browser_login": True,
        "saved_at": int(time.time()),
    }

    # Salva
    data_dir = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent.parent)))
    target_dir = data_dir / "workspaces" / workspace_slug / "sessions"
    target_dir.mkdir(parents=True, exist_ok=True)
    session_file = target_dir / f"{username}.json"
    session_file.write_text(json.dumps(session_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[browser-login] sessão salva em {session_file}")

    # Testa se funciona no instagrapi
    try:
        from instagrapi import Client
        cl = Client()
        if proxy:
            cl.set_proxy(proxy)
        cl.load_settings(str(session_file))
        cl.username = username
        cl.get_timeline_feed()
        print(f"[browser-login] sessão VÁLIDA no instagrapi!")
        return True
    except Exception as e:
        print(f"[browser-login] sessão salva mas instagrapi rejeitou: {e}")
        print(f"[browser-login] sessão pode funcionar pra post direto (sem teste)")
        return True  # Salva mesmo assim — pode funcionar pro upload
