from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from .downlink_scheduler import DownlinkScheduler

if TYPE_CHECKING:  # pragma: no cover - for type checking only
    from .lorawan import LoRaWANFrame, JoinAccept

logger = logging.getLogger(__name__)

# Paramètres ADR (valeurs issues de la spécification LoRaWAN)
REQUIRED_SNR = {7: -7.5, 8: -10.0, 9: -12.5, 10: -15.0, 11: -17.5, 12: -20.0}
MARGIN_DB = 15.0

class NetworkServer:
    """Représente le serveur de réseau LoRa (collecte des paquets reçus)."""
    def __init__(self):
        """Initialise le serveur réseau."""
        # Ensemble des identifiants d'événements déjà reçus (pour éviter les doublons)
        self.received_events = set()
        # Stockage optionnel d'infos sur les réceptions (par ex : via quelle passerelle)
        self.event_gateway = {}
        # Compteur de paquets reçus
        self.packets_received = 0
        # Indicateur ADR serveur
        self.adr_enabled = False
        # Références pour ADR serveur
        self.nodes = []
        self.gateways = []
        self.channel = None
        self.net_id = 0
        self.next_devaddr = 1
        self.scheduler = DownlinkScheduler()

    # ------------------------------------------------------------------
    # Downlink management
    # ------------------------------------------------------------------
    def send_downlink(
        self,
        node,
        payload: bytes | LoRaWANFrame | JoinAccept = b"",
        confirmed: bool = False,
        adr_command: tuple | None = None,
        request_ack: bool = False,
        at_time: float | None = None,
        gateway=None,
        ):
        """Queue a downlink frame for a node via ``gateway`` or the first one."""
        from .lorawan import (
            LoRaWANFrame,
            LinkADRReq,
            SF_TO_DR,
            DBM_TO_TX_POWER_INDEX,
            JoinAccept,
        )

        gw = gateway or (self.gateways[0] if self.gateways else None)
        if gw is None:
            return
        fctrl = 0x20 if request_ack else 0
        frame: LoRaWANFrame | JoinAccept
        if isinstance(payload, JoinAccept):
            frame = payload
        elif isinstance(payload, LoRaWANFrame):
            frame = payload
        else:
            raw = payload.to_bytes() if hasattr(payload, "to_bytes") else bytes(payload)
            frame = LoRaWANFrame(
                mhdr=0x60,
                fctrl=fctrl,
                fcnt=node.fcnt_down,
                payload=raw,
                confirmed=confirmed,
            )
        if adr_command and isinstance(frame, LoRaWANFrame):
            if len(adr_command) == 2:
                sf, power = adr_command
                chmask = node.chmask
                nbtrans = node.nb_trans
            else:
                sf, power, chmask, nbtrans = adr_command
            dr = SF_TO_DR.get(sf, 5)
            p_idx = DBM_TO_TX_POWER_INDEX.get(int(power), 0)
            frame.payload = LinkADRReq(dr, p_idx, chmask, nbtrans).to_bytes()
        if node.security_enabled and isinstance(frame, LoRaWANFrame):
            from .lorawan import encrypt_payload, compute_mic

            enc = encrypt_payload(node.appskey, node.devaddr, node.fcnt_down, 1, frame.payload)
            frame.encrypted_payload = enc
            frame.mic = compute_mic(node.nwkskey, node.devaddr, node.fcnt_down, 1, enc)
        node.fcnt_down += 1
        if at_time is None:
            gw.buffer_downlink(node.id, frame)
        else:
            self.scheduler.schedule(node.id, at_time, frame, gw)
        try:
            node.downlink_pending += 1
        except AttributeError:
            pass

    def _derive_keys(self, appkey: bytes, devnonce: int, appnonce: int) -> tuple[bytes, bytes]:
        from .lorawan import derive_session_keys

        return derive_session_keys(appkey, devnonce, appnonce, self.net_id)

    def deliver_scheduled(self, node_id: int, current_time: float) -> None:
        """Move ready scheduled frames to the gateway buffer."""
        frame, gw = self.scheduler.pop_ready(node_id, current_time)
        while frame and gw:
            gw.buffer_downlink(node_id, frame)
            frame, gw = self.scheduler.pop_ready(node_id, current_time)

    def _activate(self, node, gateway=None):
        from .lorawan import JoinAccept

        appnonce = self.next_devaddr & 0xFFFFFF
        devaddr = self.next_devaddr
        self.next_devaddr += 1
        devnonce = (node.devnonce - 1) & 0xFFFF
        nwk_skey, app_skey = self._derive_keys(node.appkey, devnonce, appnonce)
        # Store derived keys server-side but send only join parameters
        frame = JoinAccept(appnonce, self.net_id, devaddr)
        if node.security_enabled:
            from .lorawan import compute_join_mic, aes_decrypt

            msg = frame.to_bytes()
            pad = (16 - len(msg) % 16) % 16
            padded = msg + bytes(pad)
            frame.encrypted = aes_decrypt(node.appkey, padded)
            frame.mic = compute_join_mic(node.appkey, msg)
        node.nwkskey = nwk_skey
        node.appskey = app_skey
        self.send_downlink(node, frame, gateway=gateway)

    def receive(self, event_id: int, node_id: int, gateway_id: int, rssi: float | None = None):
        """
        Traite la réception d'un paquet par le serveur.
        Évite de compter deux fois le même paquet s'il arrive via plusieurs passerelles.
        :param event_id: Identifiant unique de l'événement de transmission du paquet.
        :param node_id: Identifiant du nœud source.
        :param gateway_id: Identifiant de la passerelle ayant reçu le paquet.
        :param rssi: RSSI mesuré par la passerelle pour ce paquet (optionnel).
        """
        if event_id in self.received_events:
            # Doublon (déjà reçu via une autre passerelle)
            logger.debug(f"NetworkServer: duplicate packet event {event_id} from node {node_id} (ignored).")
            return
        # Nouveau paquet reçu
        self.received_events.add(event_id)
        self.event_gateway[event_id] = gateway_id
        self.packets_received += 1
        logger.debug(f"NetworkServer: packet event {event_id} from node {node_id} received via gateway {gateway_id}.")

        node = next((n for n in self.nodes if n.id == node_id), None)
        gw = next((g for g in self.gateways if g.id == gateway_id), None)
        if node and not getattr(node, "activated", True):
            self._activate(node, gateway=gw)

        if node and node.last_adr_ack_req:
            # Device requested an ADR acknowledgement
            self.send_downlink(node)
            node.last_adr_ack_req = False

        # Appliquer ADR complet au niveau serveur
        if self.adr_enabled and rssi is not None:
            from .lorawan import DBM_TO_TX_POWER_INDEX, TX_POWER_INDEX_TO_DBM

            node = next((n for n in self.nodes if n.id == node_id), None)
            if node:
                snr = rssi - self.channel.noise_floor_dBm()
                node.snr_history.append(snr)
                if len(node.snr_history) > 20:
                    node.snr_history.pop(0)
                if len(node.snr_history) >= 20:
                    max_snr = max(node.snr_history)
                    required = REQUIRED_SNR.get(node.sf, -20.0)
                    margin = max_snr - required - MARGIN_DB
                    nstep = round(margin / 3.0)

                    sf = node.sf
                    power = node.tx_power
                    p_idx = DBM_TO_TX_POWER_INDEX.get(int(power), 0)

                    if nstep > 0:
                        while nstep > 0 and (sf > 7 or p_idx < max(TX_POWER_INDEX_TO_DBM.keys())):
                            if sf > 7:
                                sf -= 1
                            elif p_idx < max(TX_POWER_INDEX_TO_DBM.keys()):
                                p_idx += 1
                                power = TX_POWER_INDEX_TO_DBM[p_idx]
                            nstep -= 1
                    elif nstep < 0:
                        while nstep < 0 and (p_idx > 0 or sf < 12):
                            if p_idx > 0:
                                p_idx -= 1
                                power = TX_POWER_INDEX_TO_DBM[p_idx]
                            elif sf < 12:
                                sf += 1
                            nstep += 1

                    if sf != node.sf or power != node.tx_power:
                        self.send_downlink(node, adr_command=(sf, power, node.chmask, node.nb_trans))
                        node.snr_history.clear()
