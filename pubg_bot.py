# -*- coding: utf-8 -*-
"""
Bot pubg.report -> Discord (via webhook)

Avisa no canal do Discord quando alguem do esquadrao aparece na live de um
streamer (matou, derrubou, foi morto...), agrupando por partida, com os links
dos VODs da Twitch e um resumo da partida pela API oficial do PUBG.

Modos:
    python pubg_bot.py            -> roda no PC, checando a cada X minutos
    python pubg_bot.py --uma-vez  -> faz uma checagem e sai (usado no GitHub Actions)
    python pubg_bot.py --teste    -> mostra na tela o que enviaria, sem enviar nem salvar

Segredos (nunca coloque no config.json):
    DISCORD_WEBHOOK  -> link do webhook do canal
    PUBG_API_KEY     -> chave gratuita de developer.pubg.com (opcional, para o resumo)
  No GitHub ficam em Settings > Secrets. No PC, no arquivo segredos.json.
"""
import json, os, sys, time, datetime

try:
    import requests
except ImportError:
    print("Falta a biblioteca 'requests'. Rode: py -m pip install requests")
    sys.exit(1)

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

PASTA = os.path.dirname(os.path.abspath(__file__))
ARQ_CONFIG = os.path.join(PASTA, "config.json")
ARQ_VISTOS = os.path.join(PASTA, "vistos.json")
ARQ_SEGREDOS = os.path.join(PASTA, "segredos.json")
ARQ_JOGADORES = os.path.join(PASTA, "jogadores.json")
API_REPORT = "https://api.pubg.report"
API_DISCORD = "https://discord.com/api/v10"
API_PUBG = "https://api.pubg.com/shards/steam"
HEADERS = {"User-Agent": "pubg-report-discord-bot (uso pessoal)"}
NO_GITHUB = os.environ.get("GITHUB_ACTIONS") == "true"

ARMAS, MAPAS = {}, {}


def log(msg):
    print(datetime.datetime.now().strftime("[%H:%M:%S] ") + str(msg), flush=True)


def sair(msg):
    log(msg)
    if not NO_GITHUB and "--uma-vez" not in sys.argv:
        try:
            input("Enter para sair...")
        except Exception:
            pass
    sys.exit(1)


# ---------------------------------------------------------------- configuracao
def ler_json(caminho, padrao):
    if os.path.exists(caminho):
        try:
            with open(caminho, encoding="utf-8-sig") as f:
                return json.load(f)
        except Exception as e:
            log("Aviso: nao consegui ler {} ({})".format(os.path.basename(caminho), e))
    return padrao


def segredo(nome):
    valor = os.environ.get(nome, "").strip()
    if valor:
        return valor
    return str(ler_json(ARQ_SEGREDOS, {}).get(nome, "")).strip()


def carregar_config():
    if not os.path.exists(ARQ_CONFIG):
        sair("Nao achei o config.json na pasta do bot.")
    cfg = ler_json(ARQ_CONFIG, None)
    if cfg is None:
        sair("O config.json esta com erro de formatacao (confira aspas e virgulas).")
    jogadores = []
    for j in cfg.get("jogadores", []):
        if isinstance(j, str):
            j = {"pubg": j, "discord_id": ""}
        nick = str(j.get("pubg", "")).strip()
        if nick and "SEU_NICK" not in nick and "NICK_DO_AMIGO" not in nick:
            did = "".join(c for c in str(j.get("discord_id", "")) if c.isdigit())
            jogadores.append({"pubg": nick, "discord_id": did})
    cfg["jogadores_fixos"] = jogadores
    cfg["canal_registro"] = "".join(c for c in str(cfg.get("canal_registro_id", "")) if c.isdigit())
    cfg["token_bot"] = segredo("DISCORD_BOT_TOKEN")
    if not jogadores and not cfg["canal_registro"]:
        sair("Coloque um 'canal_registro_id' ou pelo menos um jogador no config.json.")
    if cfg["canal_registro"] and not cfg["token_bot"]:
        log("Aviso: ha canal de registro, mas falta o DISCORD_BOT_TOKEN; o registro fica desligado.")
    cfg["intervalo_minutos"] = max(10, int(cfg.get("intervalo_minutos", 10)))
    cfg["webhook"] = segredo("DISCORD_WEBHOOK")
    cfg["chave_pubg"] = segredo("PUBG_API_KEY")
    if "--teste" not in sys.argv and "/api/webhooks/" not in cfg["webhook"]:
        sair("Falta o link do webhook (DISCORD_WEBHOOK) nos segredos.")
    return cfg


def salvar_vistos(v):
    v["ids"] = v["ids"][-5000:]
    with open(ARQ_VISTOS, "w", encoding="utf-8") as f:
        json.dump(v, f, ensure_ascii=False, indent=0)


def carregar_dicionarios():
    base = "https://raw.githubusercontent.com/pubg/api-assets/master/dictionaries/telemetry/"
    for nome, destino in (("damageCauserName.json", ARMAS), ("mapName.json", MAPAS)):
        try:
            destino.update(requests.get(base + nome, timeout=20).json())
        except Exception:
            log("Aviso: nao consegui baixar " + nome)


# ---------------------------------------------------------------- pubg.report
def get_json(url, **kw):
    r = requests.get(url, headers=kw.pop("headers", HEADERS), timeout=kw.pop("timeout", 30), **kw)
    r.raise_for_status()
    return r.json()


def achar_conta(nick, vistos):
    if nick.lower() in vistos["contas"]:
        return vistos["contas"][nick.lower()]
    resultados = get_json(API_REPORT + "/search/" + requests.utils.quote(nick))
    steam = [x for x in resultados if x.get("shard") == "steam"]
    exato = [x for x in steam if x.get("nickname", "").lower() == nick.lower()]
    if not exato:
        log("Nao achei o nick exato '{}' no pubg.report (Steam). Confira a grafia.".format(nick))
        return None
    vistos["contas"][nick.lower()] = exato[0]["id"]
    return exato[0]["id"]


def link_twitch(ev, recuo=10):
    video = str(ev.get("VideoID", "")).lstrip("v")
    try:
        h, m, s = [int(float(x)) for x in str(ev.get("TimeDiff", "0:0:0")).split(":")]
        total = max(0, h * 3600 + m * 60 + s - recuo)  # volta uns segundos pra ver o lance
    except Exception:
        total = 0
    return "https://www.twitch.tv/videos/{}?t={}h{}m{}s".format(video, total // 3600, (total % 3600) // 60, total % 60)


def texto_link(ev, nick):
    killer, vitima = ev.get("Killer", "?"), ev.get("Victim", "?")
    nocaute = "Groggy" in ev.get("Event", "")
    eu_ataquei = killer.lower() == nick.lower()
    eu_sofri = vitima.lower() == nick.lower()
    if eu_ataquei and eu_sofri:
        return nick, ("Se nocou sozinho" if nocaute else "Se matou sozinho")
    if eu_ataquei:
        return vitima, ("Você nocou " if nocaute else "Você eliminou ") + vitima
    return killer, ("Nocado por " if nocaute else "Eliminado por ") + killer


# ---------------------------------------------------------------- API oficial
CAUSAS = {
    "Damage_BlueZone": "zona azul", "Damage_RedZone": "zona vermelha", "Damage_BlackZone": "zona preta",
    "Damage_Drown": "afogamento", "Damage_Fall": "queda", "Damage_VehicleCrashHit": "acidente de veículo",
    "Damage_Explosion_Vehicle": "explosão de veículo",
}


def resumo_partida(match_id, chave, nicks):
    """Retorna {nick_minusculo: {derrubou, matou, morto_por, vivo, posicao}} ou None."""
    if not chave:
        return None
    h = {"Accept": "application/vnd.api+json", "Authorization": "Bearer " + chave}
    try:
        r = requests.get(API_PUBG + "/matches/" + match_id, headers=h, timeout=30)
        if r.status_code == 401:
            log("Chave da API do PUBG invalida (401).")
            return None
        if r.status_code != 200:
            log("API do PUBG respondeu {} para a partida {}".format(r.status_code, match_id))
            return None
        dados = r.json()
    except Exception as e:
        log("Erro na API do PUBG: {}".format(e))
        return None

    alvo = {n.lower() for n in nicks}
    res, url_tele = {}, None
    for inc in dados.get("included", []):
        if inc.get("type") == "participant":
            s = inc.get("attributes", {}).get("stats", {})
            nome = str(s.get("name", "")).lower()
            if nome in alvo:
                res[nome] = {"derrubou": [], "matou": [], "morto_por": None,
                             "vivo": s.get("deathType") == "alive",
                             "posicao": s.get("winPlace"),
                             "n_kills": s.get("kills", 0), "n_knocks": s.get("DBNOs", 0),
                             "detalhado": False}
        elif inc.get("type") == "asset":
            url_tele = inc.get("attributes", {}).get("URL")
    if not res:
        return None

    if url_tele:
        try:
            tele = requests.get(url_tele, timeout=120).json()
            for e in tele:
                t = e.get("_T")
                if t == "LogPlayerMakeGroggy":
                    a = ((e.get("attacker") or {}).get("name") or "")
                    v = ((e.get("victim") or {}).get("name") or "")
                    if a.lower() in res and v and v.lower() != a.lower():
                        res[a.lower()]["derrubou"].append(v)
                elif t == "LogPlayerKillV2":
                    k = ((e.get("killer") or {}).get("name") or "")
                    v = ((e.get("victim") or {}).get("name") or "")
                    if k.lower() in res and v and v.lower() != k.lower():
                        res[k.lower()]["matou"].append(v)
                    if v.lower() in res:
                        causa = ((e.get("killerDamageInfo") or {}).get("damageTypeCategory") or "")
                        res[v.lower()]["morto_por"] = k or CAUSAS.get(causa, "a zona")
            for r_ in res.values():
                r_["detalhado"] = True
        except Exception as e:
            log("Nao consegui baixar a telemetria da partida ({}); usando so os numeros.".format(e))
    return res


def lista_nomes(nomes, maximo=3):
    unicos = list(dict.fromkeys(nomes))
    if len(unicos) <= maximo:
        return ", ".join(unicos)
    return "{} e mais {}".format(", ".join(unicos[:maximo]), len(unicos) - maximo)


def linha_resumo(r):
    partes = []
    if r["detalhado"]:
        if r["derrubou"]:
            partes.append("🥊 **Derrubou:** " + lista_nomes(r["derrubou"]))
        if r["matou"]:
            partes.append("🔫 **Matou:** " + lista_nomes(r["matou"]))
        if not r["derrubou"] and not r["matou"]:
            partes.append("🍿 **Só assistiu:** 0 abates")
    else:
        if r["n_kills"] or r["n_knocks"]:
            partes.append("🥊 **Derrubadas:** {} | 🔫 **Abates:** {}".format(r["n_knocks"], r["n_kills"]))
        else:
            partes.append("🍿 **Só assistiu:** 0 abates")
    if r["posicao"] == 1:
        partes.append("🍗 **Winner Winner Chicken Dinner!**")
    elif r["vivo"]:
        partes.append("🐔 **Sobreviveu!**")
    elif r["morto_por"]:
        partes.append("💀 **Eliminado por:** " + r["morto_por"])
    if r["posicao"]:
        partes.append("🏅 #{}".format(r["posicao"]))
    return "> " + " | ".join(partes)


# ---------------------------------------------------------------- mensagem
def montar_mensagem(partida, cfg, resumo):
    mapa_id = partida["mapa"]
    mapa = MAPAS.get(mapa_id, mapa_id)
    linhas = ["Veja se o esquadrão apareceu na transmissão de algum streamer:", ""]
    mencoes = []
    for j in cfg["jogadores"]:
        evs = partida["eventos"].get(j["pubg"])
        if not evs:
            continue
        nome = "<@{}>".format(j["discord_id"]) if j["discord_id"] else "**{}**".format(j["pubg"])
        if j["discord_id"]:
            mencoes.append(j["discord_id"])
        links = []
        for ev in evs[:10]:
            streamer, desc = texto_link(ev, j["pubg"])
            links.append("[Assistir VOD de {} na Twitch ({})]({})".format(streamer, desc, link_twitch(ev)))
        if len(links) == 1:
            linhas.append("🎬 {} - {}".format(nome, links[0]))
        else:
            linhas.append("🎬 {}".format(nome))
            linhas.extend("• " + l for l in links)
        if resumo and j["pubg"].lower() in resumo:
            linhas.append(linha_resumo(resumo[j["pubg"].lower()]))
        linhas.append("")
    descricao = "\n".join(linhas).strip()
    if len(descricao) > 4000:
        descricao = descricao[:3990] + "\n…"
    titulo = "🎥 PUBG.report - {}".format(mapa)
    if partida.get("atualizacao"):
        titulo += " (mais clipes)"
    embed = {
        "title": titulo,
        "description": descricao,
        "color": 0xF2A900,
        "footer": {"text": "pubg.report • {} • {}".format(partida["modo"], partida["data"])},
    }
    if partida.get("quando"):
        embed["timestamp"] = partida["quando"]
    corpo = {"username": cfg.get("nome_do_bot", "PUBG Report"), "embeds": [embed],
             "allowed_mentions": {"users": mencoes}}
    if mencoes:
        corpo["content"] = " ".join("<@{}>".format(m) for m in mencoes)
    return corpo


def enviar(cfg, corpo):
    for _ in range(5):
        r = requests.post(cfg["webhook"], json=corpo, timeout=30)
        if r.status_code == 429:
            try:
                espera = float(r.json().get("retry_after", 2))
            except Exception:
                espera = 2
            time.sleep(espera + 0.5)
            continue
        if r.status_code >= 400:
            log("Erro ao enviar para o Discord ({}): {}".format(r.status_code, r.text[:300]))
        return r.status_code < 400
    return False


# ---------------------------------------------------------------- registro pelo Discord
def discord(cfg, metodo, caminho, **kw):
    h = {"Authorization": "Bot " + cfg["token_bot"],
         "User-Agent": "DiscordBot (https://github.com, 1.0)"}
    for _ in range(4):
        r = requests.request(metodo, API_DISCORD + caminho, headers=h, timeout=30, **kw)
        if r.status_code == 429:
            try:
                time.sleep(float(r.json().get("retry_after", 1)) + 0.3)
            except Exception:
                time.sleep(2)
            continue
        return r
    return r


def reagir(cfg, msg_id, emoji):
    discord(cfg, "PUT", "/channels/{}/messages/{}/reactions/{}/@me".format(
        cfg["canal_registro"], msg_id, requests.utils.quote(emoji)))
    time.sleep(0.4)


def responder(cfg, msg_id, texto):
    discord(cfg, "POST", "/channels/{}/messages".format(cfg["canal_registro"]),
            json={"content": texto, "message_reference": {"message_id": msg_id},
                  "allowed_mentions": {"parse": []}})


def processar_registros(cfg, vistos, registrados):
    """Le o canal de registro e trata !registrar <nick> e !sair."""
    if not (cfg["canal_registro"] and cfg["token_bot"]):
        return
    ultimo = vistos.get("ultimo_msg_registro", "0")
    params = {"limit": 100}
    if ultimo != "0":
        params["after"] = ultimo
    r = discord(cfg, "GET", "/channels/{}/messages".format(cfg["canal_registro"]), params=params)
    if r.status_code == 401:
        log("DISCORD_BOT_TOKEN invalido (401).")
        return
    if r.status_code == 403:
        log("O bot nao tem permissao para ver o canal de registro (403). Confira as permissoes do canal.")
        return
    if r.status_code != 200:
        log("Erro ao ler o canal de registro ({}): {}".format(r.status_code, r.text[:200]))
        return
    mensagens = sorted(r.json(), key=lambda m: int(m["id"]))
    sem_texto = 0
    for m in mensagens:
        vistos["ultimo_msg_registro"] = m["id"]
        if m.get("author", {}).get("bot"):
            continue
        texto = (m.get("content") or "").strip()
        if not texto:
            sem_texto += 1
            continue
        partes = texto.split()
        comando = partes[0].lower()
        uid = m["author"]["id"]
        if comando in ("!registrar", "!register"):
            if len(partes) < 2:
                reagir(cfg, m["id"], "❌")
                responder(cfg, m["id"], "Use assim: `!registrar SeuNickDoPUBG`")
                continue
            nick = partes[1]
            try:
                conta = achar_conta(nick, vistos)
            except Exception as e:
                log("Erro ao validar {}: {}".format(nick, e))
                vistos["ultimo_msg_registro"] = str(int(m["id"]) - 1)  # tenta de novo na proxima
                break
            if not conta:
                reagir(cfg, m["id"], "❌")
                responder(cfg, m["id"], "Não achei o nick **{}** no pubg.report (Steam). "
                          "Confira se está igual ao do jogo.".format(nick))
                continue
            registrados[uid] = {"pubg": nick, "novo": True}
            reagir(cfg, m["id"], "✅")
            log("Registrado: {} -> {}".format(m["author"].get("username", uid), nick))
        elif comando in ("!sair", "!unregister"):
            if registrados.pop(uid, None):
                reagir(cfg, m["id"], "👋")
                log("Saiu: {}".format(m["author"].get("username", uid)))
            else:
                reagir(cfg, m["id"], "❓")
    if sem_texto and sem_texto == len([m for m in mensagens if not m.get("author", {}).get("bot")]):
        log("Aviso: as mensagens chegaram sem texto. Ative 'Message Content Intent' no Developer Portal.")


def montar_lista_jogadores(cfg, registrados):
    lista, ja = [], set()
    for did, info in registrados.items():
        n = info["pubg"]
        if n.lower() not in ja:
            lista.append({"pubg": n, "discord_id": did})
            ja.add(n.lower())
    for j in cfg["jogadores_fixos"]:
        if j["pubg"].lower() not in ja:
            lista.append(j)
            ja.add(j["pubg"].lower())
    return lista


# ---------------------------------------------------------------- rodada
def rodada(cfg, vistos, primeira_vez, teste, novos=()):
    partidas, ok = {}, set()
    for j in cfg["jogadores"]:
        nick = j["pubg"]
        try:
            conta = achar_conta(nick, vistos)
            if not conta:
                continue
            time.sleep(1)
            dados = get_json(API_REPORT + "/v1/players/" + conta + "/streams")
        except Exception as e:
            log("Erro ao consultar pubg.report para {}: {}".format(nick, e))
            continue
        total = 0
        if isinstance(dados, dict):
            for mid, lista in dados.items():
                for ev in lista or []:
                    total += 1
                    if not ev.get("ID") or ev["ID"] in vistos["ids"]:
                        continue
                    if nick.lower() in novos and not teste:
                        vistos["ids"].append(ev["ID"])  # jogador recem-registrado: nao manda o historico
                        continue
                    p = partidas.setdefault(mid, {"id": mid, "eventos": {}, "mapa": ev.get("Map", "?"),
                                                  "modo": ev.get("Mode", "?"), "data": ev.get("Date", ""),
                                                  "quando": ""})
                    p["eventos"].setdefault(nick, []).append(ev)
                    p["quando"] = max(p["quando"], ev.get("TimeEvent", ""))
        ok.add(nick.lower())
        log("{}: {} lance(s) no pubg.report".format(nick, total))

    enviados = 0
    for p in sorted(partidas.values(), key=lambda x: x["quando"]):
        ids = [ev["ID"] for evs in p["eventos"].values() for ev in evs]
        for evs in p["eventos"].values():
            evs.sort(key=lambda e: e.get("TimeEvent", ""))
        if primeira_vez and not cfg.get("avisar_historico", False) and not teste:
            vistos["ids"].extend(ids)
            continue
        p["atualizacao"] = p["id"] in vistos.get("partidas", [])
        nicks = list(p["eventos"].keys())
        # resumo considera todo o esquadrao cadastrado, nao so quem apareceu
        resumo = resumo_partida(p["id"], cfg["chave_pubg"], [j["pubg"] for j in cfg["jogadores"]])
        corpo = montar_mensagem(p, cfg, resumo)
        if teste:
            print("\n" + "=" * 60)
            print(corpo["embeds"][0]["title"])
            print(corpo["embeds"][0]["description"])
            continue
        if enviar(cfg, corpo):
            vistos["ids"].extend(ids)
            vistos.setdefault("partidas", []).append(p["id"])
            vistos["partidas"] = vistos["partidas"][-500:]
            enviados += 1
            time.sleep(1.5)
    if primeira_vez and not cfg.get("avisar_historico", False) and not teste:
        log("Primeira execucao: lances antigos marcados como vistos (nao enviados).")
    elif enviados:
        log("{} partida(s) enviada(s) ao Discord.".format(enviados))
    else:
        log("Nada novo.")
    return ok


def main():
    teste = "--teste" in sys.argv
    uma_vez = "--uma-vez" in sys.argv or NO_GITHUB
    cfg = carregar_config()
    primeira_vez = not os.path.exists(ARQ_VISTOS)
    vistos = ler_json(ARQ_VISTOS, {})
    vistos.setdefault("ids", [])
    vistos.setdefault("contas", {})
    vistos.setdefault("partidas", [])
    registrados = ler_json(ARQ_JOGADORES, {})
    if not cfg["chave_pubg"]:
        log("Sem PUBG_API_KEY: as mensagens vao sem o resumo da partida.")
    carregar_dicionarios()
    if teste:
        log("MODO TESTE: nada sera enviado nem salvo.")
        cfg["jogadores"] = montar_lista_jogadores(cfg, registrados)
        log("Jogadores: " + (", ".join(j["pubg"] for j in cfg["jogadores"]) or "nenhum"))
        rodada(cfg, vistos, False, True)
        return
    while True:
        processar_registros(cfg, vistos, registrados)
        cfg["jogadores"] = montar_lista_jogadores(cfg, registrados)
        novos = {i["pubg"].lower() for i in registrados.values() if i.get("novo")}
        log("Jogadores: " + (", ".join(j["pubg"] for j in cfg["jogadores"]) or "nenhum ainda"))
        ok = rodada(cfg, vistos, primeira_vez, False, novos) if cfg["jogadores"] else set()
        for i in registrados.values():
            if i["pubg"].lower() in ok:
                i.pop("novo", None)
        salvar_vistos(vistos)
        with open(ARQ_JOGADORES, "w", encoding="utf-8") as f:
            json.dump(registrados, f, ensure_ascii=False, indent=1)
        primeira_vez = False
        if uma_vez:
            return
        log("Proxima checagem em {} minutos. (Feche a janela para parar.)".format(cfg["intervalo_minutos"]))
        time.sleep(cfg["intervalo_minutos"] * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Bot encerrado.")
