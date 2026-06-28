from dataclasses import dataclass


@dataclass
class DetectorConfig:
    # Phase 1: baseline
    baseline_len: int = 40
    std_floor: float = 1e-6

    # Phase 2: windowed drift check
    window: int = 25
    drop_threshold: float = 0.20
    min_sustained: int = 5

    # Phase 4: failing run collection
    failing_ids_cap: int = 8
