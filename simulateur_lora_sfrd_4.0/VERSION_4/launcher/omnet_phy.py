"""Simplified OMNeT++ physical layer helpers."""

from __future__ import annotations

import math
import random

from .omnet_model import OmnetModel


class OmnetPHY:
    """Replicate OMNeT++ FLoRa PHY calculations."""

    def __init__(self, channel) -> None:
        self.channel = channel
        self.model = OmnetModel(
            channel.fine_fading_std,
            channel.omnet.correlation,
            channel.omnet.noise_std,
            freq_drift_std=channel.omnet.freq_drift_std,
            clock_drift_std=channel.omnet.clock_drift_std,
            temperature_K=channel.omnet.temperature_K,
        )

    # ------------------------------------------------------------------
    def path_loss(self, distance: float) -> float:
        """Return path loss in dB using the log distance model."""
        if distance <= 0:
            return 0.0
        freq_mhz = self.channel.frequency_hz / 1e6
        pl_d0 = 32.45 + 20 * math.log10(freq_mhz) - 60.0
        loss = pl_d0 + 10 * self.channel.path_loss_exp * math.log10(max(distance, 1.0))
        return loss + self.channel.system_loss_dB

    def noise_floor(self) -> float:
        """Return the noise floor (dBm) including optional variations."""
        ch = self.channel
        thermal = self.model.thermal_noise_dBm(ch.bandwidth)
        noise = thermal + ch.noise_figure_dB + ch.interference_dB
        if ch.noise_floor_std > 0:
            noise += random.gauss(0.0, ch.noise_floor_std)
        noise += self.model.noise_variation()
        return noise

    def compute_rssi(
        self,
        tx_power_dBm: float,
        distance: float,
        sf: int | None = None,
        *,
        freq_offset_hz: float | None = None,
        sync_offset_s: float | None = None,
    ) -> tuple[float, float]:
        ch = self.channel
        loss = self.path_loss(distance)
        if ch.shadowing_std > 0:
            loss += random.gauss(0.0, ch.shadowing_std)
        rssi = (
            tx_power_dBm
            + ch.tx_antenna_gain_dB
            + ch.rx_antenna_gain_dB
            - loss
            - ch.cable_loss_dB
        )
        if ch.tx_power_std > 0:
            rssi += random.gauss(0.0, ch.tx_power_std)
        if ch.fast_fading_std > 0:
            rssi += random.gauss(0.0, ch.fast_fading_std)
        if ch.time_variation_std > 0:
            rssi += random.gauss(0.0, ch.time_variation_std)
        rssi += self.model.fine_fading()
        rssi += ch.rssi_offset_dB

        if freq_offset_hz is None:
            freq_offset_hz = ch.frequency_offset_hz
        if sync_offset_s is None:
            sync_offset_s = ch.sync_offset_s

        snr = rssi - self.noise_floor() + ch.snr_offset_dB
        penalty = self._alignment_penalty_db(freq_offset_hz, sync_offset_s, sf)
        snr -= penalty
        if sf is not None:
            snr += 10 * math.log10(2 ** sf)
        return rssi, snr

    def _alignment_penalty_db(
        self, freq_offset_hz: float, sync_offset_s: float, sf: int | None
    ) -> float:
        """Return SNR penalty for imperfect alignment."""
        bw = self.channel.bandwidth
        freq_factor = abs(freq_offset_hz) / (bw / 2.0)
        if sf is not None:
            symbol_time = (2 ** sf) / bw
        else:
            symbol_time = 1.0 / bw
        time_factor = abs(sync_offset_s) / symbol_time
        if freq_factor >= 1.0 and time_factor >= 1.0:
            return float("inf")
        return 10 * math.log10(1.0 + freq_factor ** 2 + time_factor ** 2)

    def capture(self, rssi_list: list[float]) -> list[bool]:
        """Return list of booleans indicating which signals are captured."""
        if not rssi_list:
            return []
        order = sorted(range(len(rssi_list)), key=lambda i: rssi_list[i], reverse=True)
        winners = [False] * len(rssi_list)
        if len(order) == 1:
            winners[order[0]] = True
            return winners
        if rssi_list[order[0]] - rssi_list[order[1]] >= self.channel.capture_threshold_dB:
            winners[order[0]] = True
        return winners

