import sys
import random
from pathlib import Path

import pytest

pytest.importorskip("pandas")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from VERSION_4.launcher.channel import Channel  # noqa: E402
from VERSION_4.launcher.simulator import Simulator  # noqa: E402
from VERSION_4.launcher.omnet_phy import OmnetPHY  # noqa: E402
from VERSION_4.launcher.compare_flora import load_flora_rx_stats  # noqa: E402


def _make_colliding_sim() -> Simulator:
    ch = Channel(
        shadowing_std=0,
        fast_fading_std=0,
        fine_fading_std=1.0,
        variable_noise_std=0.5,
        phy_model="omnet",
    )
    sim = Simulator(
        num_nodes=2,
        num_gateways=1,
        area_size=10.0,
        transmission_mode="Periodic",
        packet_interval=10.0,
        packets_to_send=1,
        mobility=False,
        duty_cycle=None,
        channels=[ch],
        fixed_sf=7,
        fixed_tx_power=14.0,
        phy_model="omnet",
    )
    gw = sim.gateways[0]
    for node in sim.nodes:
        node.x = gw.x
        node.y = gw.y
    sim.event_queue.clear()
    sim.event_id_counter = 0
    for node in sim.nodes:
        sim.schedule_event(node, 0.0)
    return sim


def test_omnet_phy_matches_flora():
    random.seed(0)
    sim = _make_colliding_sim()
    while sim.step():
        pass
    rssi = sim.nodes[0].last_rssi
    snr = sim.nodes[0].last_snr
    flora_csv = Path(__file__).parent / "data" / "flora_rx_stats.csv"
    flora = load_flora_rx_stats(flora_csv)
    assert rssi == pytest.approx(flora["rssi"], abs=1e-3)
    assert snr == pytest.approx(flora["snr"], abs=1e-3)
    assert sim.packets_lost_collision == flora["collisions"]


def test_device_specific_features_in_phy():
    random.seed(0)
    ch = Channel(shadowing_std=0, phy_model="omnet")
    ch.omnet_phy = OmnetPHY(
        ch,
        dev_frequency_offset_hz=500.0,
        dev_freq_offset_std_hz=100.0,
        temperature_std_K=20.0,
        pa_non_linearity_std_dB=1.0,
        phase_noise_std_dB=2.0,
        oscillator_leakage_dB=1.0,
        oscillator_leakage_std_dB=0.5,
        rx_fault_std_dB=1.0,
    )
    r1, _ = ch.omnet_phy.compute_rssi(14.0, 100.0)
    r2, _ = ch.omnet_phy.compute_rssi(14.0, 100.0)
    assert r1 != r2


def test_oscillator_leakage_affects_noise():
    ch = Channel(shadowing_std=0, phy_model="omnet")
    base_noise = ch.omnet_phy.noise_floor()
    ch.omnet_phy = OmnetPHY(
        ch,
        oscillator_leakage_dB=5.0,
    )
    leak_noise = ch.omnet_phy.noise_floor()
    assert leak_noise > base_noise
