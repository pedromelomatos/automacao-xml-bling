"""Registro persistente de entregas e exclusão mútua no Redis.

Importar este módulo não carrega redis-py nem acessa a rede. A simulação pode
ler o registro, mas não deve adquirir ``bloqueio``: sua aquisição recupera
tentativas interrompidas, marcando-as como resultado incerto.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import re
import secrets
import threading


ESTADOS = frozenset({"pendente", "em_envio", "entregue", "falha", "incerto", "revisao"})
CAMPOS_IDENTIDADE = ("sha256", "destino", "cnpj_emitente", "canal_bling")


class RegistroIndisponivel(RuntimeError):
    """O controle não pode ser lido ou gravado; novos envios devem parar."""


class BloqueioOcupado(RegistroIndisponivel):
    """Outra execução mantém o bloqueio global."""


class BloqueioPerdido(RegistroIndisponivel):
    """A execução perdeu a posse do bloqueio; não pode continuar enviando."""


def _agora():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _chave(valor):
    if not isinstance(valor, str) or not re.fullmatch(r"\d{44}", valor, flags=re.ASCII):
        raise ValueError("Chave da NF-e deve conter exatamente 44 dígitos ASCII.")
    return valor


def _normalizar(registro):
    if not isinstance(registro, dict):
        raise ValueError("Registro de entrega deve ser um objeto.")
    dados = dict(registro)
    dados["chave"] = _chave(dados.get("chave"))
    for campo in ("canal_bling", "cnpj_emitente"):
        if dados.get(campo) is not None:
            dados[campo] = str(dados[campo])
    if dados.get("sha256"):
        if not isinstance(dados["sha256"], str) or not re.fullmatch(r"[a-fA-F0-9]{64}", dados["sha256"]):
            raise ValueError("SHA-256 inválido no registro de entrega.")
        dados["sha256"] = dados["sha256"].lower()
    # Garante que os dados cabem no formato persistido antes de abrir transação.
    json.dumps(dados, ensure_ascii=False, allow_nan=False)
    return dados


class _Bloqueio:
    def __init__(self, registro, token, ttl):
        self._registro = registro
        self._token = token
        self._ttl = ttl
        self._thread = threading.get_ident()

    def verificar(self):
        if threading.get_ident() != self._thread:
            raise BloqueioPerdido("O bloqueio pertence a outra thread.")
        try:
            dono = self._registro._cliente.get(self._registro._lock_key)
        except Exception as erro:
            raise RegistroIndisponivel("Não foi possível conferir o bloqueio de entregas.") from erro
        if dono != self._token:
            raise BloqueioPerdido("A execução perdeu o bloqueio de entregas.")
        return True

    def renovar(self, ttl=None):
        self._registro._alterar_bloqueio(self, ttl=self._ttl if ttl is None else ttl)
        return True


class RegistroEntregasRedis:
    """Ledger sem expiração automática dos registros das notas.

    ``cliente`` permite injetar um cliente compatível com redis-py nos testes.
    O URL nunca é incluído em mensagens de erro, pois pode conter senha.
    """

    def __init__(self, url, prefixo="bling:dropbox", *, cliente=None):
        try:
            import redis
        except ImportError as erro:
            raise RegistroIndisponivel("Instale a dependência redis para habilitar a integração.") from erro
        if not isinstance(prefixo, str) or not prefixo.strip():
            raise ValueError("Prefixo Redis não pode ser vazio.")
        self._cliente = cliente if cliente is not None else redis.Redis.from_url(
            url, decode_responses=True, socket_connect_timeout=5,
            socket_timeout=5, health_check_interval=30,
        )
        self._watch_error = redis.WatchError
        self._prefixo = prefixo.rstrip(":")
        self._index_key = f"{self._prefixo}:chaves"
        self._lock_key = f"{self._prefixo}:bloqueio"
        self._cooldown_key = f"{self._prefixo}:proximo_lote_em"
        self._eventos_key = f"{self._prefixo}:eventos"
        self._ativo = None

    def _key(self, chave):
        return f"{self._prefixo}:nfe:{_chave(chave)}"

    def ping(self):
        try:
            return bool(self._cliente.ping())
        except Exception as erro:
            raise RegistroIndisponivel("Redis indisponível; entregas suspensas.") from erro

    @staticmethod
    def _decodificar(raw):
        if raw is None:
            return None
        try:
            dados = json.loads(raw)
            _chave(dados.get("chave"))
            if dados.get("estado") not in ESTADOS:
                raise ValueError("Estado desconhecido")
            return dados
        except (ValueError, TypeError, AttributeError) as erro:
            raise RegistroIndisponivel("Registro Redis inválido; revisão necessária.") from erro

    def obter(self, chave):
        try:
            dados = self._decodificar(self._cliente.get(self._key(chave)))
            if dados is not None and dados["chave"] != chave:
                raise RegistroIndisponivel("Chave divergente no registro Redis.")
            return dados
        except (ValueError, RegistroIndisponivel):
            raise
        except Exception as erro:
            raise RegistroIndisponivel("Não foi possível consultar o registro de entrega.") from erro

    def listar(self):
        try:
            chaves = sorted(self._cliente.smembers(self._index_key))
            if not chaves:
                return []
            valores = self._cliente.mget([self._key(chave) for chave in chaves])
            dados = [self._decodificar(valor) for valor in valores]
            if any(dado is None or dado["chave"] != chave for chave, dado in zip(chaves, dados)):
                raise RegistroIndisponivel("Índice de entregas inconsistente; revisão necessária.")
            return sorted(dados, key=lambda d: (str(d.get("emissao") or ""), d["chave"]))
        except RegistroIndisponivel:
            raise
        except Exception as erro:
            raise RegistroIndisponivel("Não foi possível listar as entregas.") from erro

    def _mutar(self, chave, transformar):
        key = self._key(chave)
        for _ in range(32):
            try:
                with self._cliente.pipeline() as pipe:
                    assistido = self._ativo
                    watch_keys = [key] + ([self._lock_key] if assistido is not None else [])
                    pipe.watch(*watch_keys)
                    if assistido is not None:
                        if threading.get_ident() != assistido._thread or pipe.get(self._lock_key) != assistido._token:
                            raise BloqueioPerdido("A execução perdeu o bloqueio; gravação cancelada.")
                    anterior = self._decodificar(pipe.get(key))
                    if anterior is not None and anterior["chave"] != chave:
                        raise RegistroIndisponivel("Chave divergente no registro Redis.")
                    novo = transformar(dict(anterior) if anterior is not None else None)
                    novo = _normalizar(novo)
                    if novo.get("estado") not in ESTADOS:
                        raise ValueError("Estado de entrega inválido.")
                    if novo == anterior:
                        return novo
                    novo["atualizado_em"] = _agora()
                    payload = json.dumps(novo, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                    pipe.multi()
                    pipe.set(key, payload)
                    pipe.sadd(self._index_key, chave)
                    pipe.xadd(self._eventos_key, {
                        "chave": chave,
                        "estado_anterior": anterior["estado"] if anterior is not None else "",
                        "estado": novo["estado"],
                        "instante": novo["atualizado_em"],
                    })
                    pipe.execute()
                    return novo
            except self._watch_error:
                continue
            except (ValueError, RegistroIndisponivel):
                raise
            except Exception as erro:
                raise RegistroIndisponivel("Falha ao persistir entrega; novos envios devem parar.") from erro
        raise RegistroIndisponivel("Concorrência excessiva ao gravar entrega; tente novamente depois.")

    def registrar(self, registro):
        dados = _normalizar(registro)
        chave = dados["chave"]
        return self._mutar(chave, lambda anterior: self._mesclar_registro(anterior, dados))

    @staticmethod
    def _mesclar_registro(anterior, dados):
        if anterior is None:
            return {**dados, "estado": "pendente", "tentativas": 0, "criado_em": _agora()}
        conflitos = {campo: {"registrado": anterior[campo], "recebido": dados[campo]}
                     for campo in CAMPOS_IDENTIDADE
                     if anterior.get(campo) not in (None, "") and dados.get(campo) not in (None, "")
                     and anterior[campo] != dados[campo]}
        if conflitos:
            novo = dict(anterior)
            novo["estado"] = "revisao"
            novo["conflito_identidade"] = conflitos
            if anterior["estado"] == "entregue":
                novo["entrega_anterior_confirmada"] = True
            return novo
        novo = dict(anterior)
        # Identidade existente é preservada; seeds podem ganhar hash e origem local.
        for campo, valor in dados.items():
            if campo in {"chave", "estado", "tentativas", "criado_em", "atualizado_em"}:
                continue
            if novo.get(campo) in (None, "") and valor not in (None, ""):
                novo[campo] = valor
            elif campo == "caminho_xml" and valor and valor != novo.get(campo):
                novo[campo] = valor
        return novo

    def atualizar(self, chave, **campos):
        chave = _chave(chave)
        if "chave" in campos and campos["chave"] != chave:
            raise ValueError("Não é permitido alterar a chave da nota.")
        normalizados = _normalizar({"chave": chave, **campos})
        normalizados.pop("chave", None)

        def transformar(anterior):
            if anterior is None:
                raise ValueError("Entrega não cadastrada.")
            novo = dict(anterior)
            for campo in CAMPOS_IDENTIDADE:
                if campo in normalizados and anterior.get(campo) not in (None, "") and normalizados[campo] != anterior[campo]:
                    raise ValueError("Identidade da entrega não pode ser alterada; registre para revisão.")
            estado = normalizados.get("estado", anterior["estado"])
            if estado not in ESTADOS:
                raise ValueError("Estado de entrega inválido.")
            if anterior["estado"] == "entregue" and estado not in {"entregue", "revisao"}:
                raise ValueError("Uma entrega concluída não pode voltar automaticamente à fila.")
            if anterior["estado"] in {"incerto", "revisao"} and estado in {"pendente", "falha", "em_envio"}:
                raise ValueError("Entrega incerta ou em revisão exige conciliação, sem reenvio automático.")
            tentativas = normalizados.get("tentativas", anterior.get("tentativas", 0))
            if not isinstance(tentativas, int) or isinstance(tentativas, bool) or tentativas < anterior.get("tentativas", 0):
                raise ValueError("Contador de tentativas inválido.")
            novo.update(normalizados)
            if estado == "em_envio" and anterior["estado"] != "em_envio" and "tentativas" not in normalizados:
                novo["tentativas"] = tentativas + 1
            return novo

        return self._mutar(chave, transformar)

    def marcar_preexistente(self, registro, evidencia):
        if evidencia is None or evidencia == "" or evidencia == [] or evidencia == {}:
            raise ValueError("Entrega preexistente exige evidência.")
        dados = _normalizar(registro)

        def transformar(anterior):
            novo = self._mesclar_registro(anterior, dados)
            if novo["estado"] != "revisao":
                novo.update(estado="entregue", evidencia_preexistente=evidencia,
                            origem_entrega="dropbox_preexistente")
            return novo

        # Não há etapa intermediária persistida como pendente para uma nota que
        # já sabemos entregue: seed, índice e evento são uma única transação.
        return self._mutar(dados["chave"], transformar)

    def _alterar_bloqueio(self, bloqueio, *, ttl=None, liberar=False):
        if threading.get_ident() != bloqueio._thread:
            raise BloqueioPerdido("O bloqueio pertence a outra thread.")
        if not liberar:
            if not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or ttl <= 0:
                raise ValueError("TTL do bloqueio deve ser positivo.")
        for _ in range(32):
            try:
                with self._cliente.pipeline() as pipe:
                    pipe.watch(self._lock_key)
                    if pipe.get(self._lock_key) != bloqueio._token:
                        raise BloqueioPerdido("A execução perdeu o bloqueio de entregas.")
                    pipe.multi()
                    if liberar:
                        pipe.delete(self._lock_key)
                    else:
                        pipe.pexpire(self._lock_key, max(1, int(ttl * 1000)))
                    pipe.execute()
                    return
            except self._watch_error:
                continue
            except BloqueioPerdido:
                raise
            except Exception as erro:
                raise RegistroIndisponivel("Não foi possível atualizar o bloqueio de entregas.") from erro
        raise RegistroIndisponivel("Concorrência excessiva ao atualizar bloqueio.")

    @contextmanager
    def bloqueio(self, ttl=300):
        if not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("TTL do bloqueio deve ser positivo.")
        if self._ativo is not None:
            raise BloqueioOcupado("Esta instância já mantém um bloqueio.")
        token = secrets.token_hex(32)
        try:
            adquiriu = self._cliente.set(self._lock_key, token, nx=True, px=max(1, int(ttl * 1000)))
        except Exception as erro:
            raise RegistroIndisponivel("Redis indisponível; nenhum envio deve começar.") from erro
        if not adquiriu:
            raise BloqueioOcupado("Outra execução está processando as entregas.")
        bloqueio = _Bloqueio(self, token, ttl)
        self._ativo = bloqueio
        erro_em_curso = False
        try:
            self.recuperar_interrompidas()
            yield bloqueio
        except BaseException:
            erro_em_curso = True
            raise
        finally:
            try:
                self._alterar_bloqueio(bloqueio, liberar=True)
            except RegistroIndisponivel:
                if not erro_em_curso:
                    raise
            finally:
                self._ativo = None

    def recuperar_interrompidas(self):
        if self._ativo is None:
            raise BloqueioPerdido("A recuperação exige bloqueio global.")
        self._ativo.verificar()
        recuperadas = 0
        for dado in self.listar():
            if dado["estado"] == "em_envio":
                self.atualizar(dado["chave"], estado="incerto",
                               ultimo_erro="Execução anterior interrompida durante o envio; conciliar antes de repetir.")
                recuperadas += 1
        return recuperadas

    def obter_proximo_lote_em(self):
        try:
            raw = self._cliente.get(self._cooldown_key)
            valor = float(raw) if raw is not None else 0.0
            if not math.isfinite(valor) or valor < 0:
                raise ValueError("Instante inválido")
            return valor
        except Exception as erro:
            raise RegistroIndisponivel("Não foi possível consultar o intervalo entre lotes.") from erro

    def definir_proximo_lote_em(self, epoch):
        if not isinstance(epoch, (int, float)) or not math.isfinite(epoch) or epoch < 0:
            raise ValueError("Próximo lote deve ter um instante válido.")
        bloqueio = self._ativo
        if bloqueio is None:
            raise BloqueioPerdido("Atualizar o intervalo exige bloqueio global.")
        bloqueio.verificar()
        for _ in range(32):
            try:
                with self._cliente.pipeline() as pipe:
                    pipe.watch(self._lock_key, self._cooldown_key)
                    if pipe.get(self._lock_key) != bloqueio._token:
                        raise BloqueioPerdido("A execução perdeu o bloqueio; intervalo não alterado.")
                    anterior = pipe.get(self._cooldown_key)
                    atual = float(anterior) if anterior is not None else 0.0
                    if not math.isfinite(atual) or atual < 0:
                        raise RegistroIndisponivel("Intervalo Redis inválido; revisão necessária.")
                    # Uma execução atrasada não pode antecipar o próximo lote.
                    proximo = max(float(epoch), atual)
                    pipe.multi()
                    pipe.set(self._cooldown_key, repr(proximo))
                    pipe.execute()
                    return proximo
            except self._watch_error:
                continue
            except RegistroIndisponivel:
                raise
            except Exception as erro:
                raise RegistroIndisponivel("Falha ao persistir o intervalo entre lotes.") from erro
        raise RegistroIndisponivel("Concorrência excessiva ao atualizar intervalo.")
