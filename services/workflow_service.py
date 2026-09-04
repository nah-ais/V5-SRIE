"""
services/workflow_service.py
-----------------------------
State machine ringan untuk alur kerja wizard 6-langkah di UI:

    ① Muat Data -> ② Pemeriksaan -> ③ Review Login -> ④ Review Register
    -> ⑤ Finalisasi -> ⑥ Export

Modul ini HANYA mengelola `st.session_state` terkait posisi/status langkah
(bukan data Login/Register itu sendiri — itu tetap dikelola lewat key-key
di config.py, lihat reset_after_new_data()).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

import config


# Urutan langkah wizard: (key_internal, label_yang_ditampilkan)
STEPS = [
    ("load", "① Muat Data"),
    ("check", "② Pemeriksaan"),
    ("review_login", "③ Review Login"),
    ("review_register", "④ Review Register"),
    ("finalize", "⑤ Finalisasi"),
    ("export", "⑥ Export"),
]


def init_workflow_state() -> None:
    """Inisialisasi key session_state khusus wizard (idempotent — aman dipanggil berkali-kali)."""
    defaults = {
        "current_step": "load",
        "matching_completed": False,
        "login_review_index": 0,
        "register_review_index": 0,
        "last_register_decision_info": "",
        "data_source_loaded": False,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def reset_after_new_data() -> None:
    """
    Dipanggil setiap kali dataset Login/Register baru dimuat (dari API atau CSV).
    Membersihkan seluruh hasil pemeriksaan & keputusan review sebelumnya,
    lalu mengarahkan wizard ke langkah "② Pemeriksaan".
    """
    st.session_state[config.SS_DUPLICATE_PAIRS_LOGIN] = pd.DataFrame()
    st.session_state[config.SS_DUPLICATE_PAIRS_REGISTER] = pd.DataFrame()
    st.session_state[config.SS_NOT_LOGIN_YET] = pd.DataFrame()
    st.session_state[config.SS_REVIEW_DECISIONS_LOGIN] = {}
    st.session_state[config.SS_REVIEW_DECISIONS_REGISTER] = {}
    st.session_state[config.SS_APPENDED_IDS] = set()

    st.session_state["matching_completed"] = False
    st.session_state["login_review_index"] = 0
    st.session_state["register_review_index"] = 0
    st.session_state["last_register_decision_info"] = ""
    st.session_state["data_source_loaded"] = True
    st.session_state["current_step"] = "check"


def go_to(step: str) -> None:
    """Pindahkan wizard ke langkah tertentu (key harus salah satu dari STEPS)."""
    st.session_state["current_step"] = step


def reviewed_count(pairs: pd.DataFrame, decisions: dict) -> int:
    """Jumlah pasangan duplikat yang sudah punya keputusan panitia."""
    if pairs is None or pairs.empty:
        return 0
    return sum(1 for pair_id in pairs["pair_id"] if pair_id in decisions)


def all_reviewed(pairs: pd.DataFrame, decisions: dict) -> bool:
    """True jika seluruh pasangan (atau tidak ada pasangan sama sekali) sudah direview."""
    return pairs is None or pairs.empty or reviewed_count(pairs, decisions) >= len(pairs)


def step_status(step_key: str) -> str:
    """Status satu langkah wizard relatif terhadap langkah aktif: 'done' / 'active' / 'todo'."""
    current = st.session_state["current_step"]
    order = [key for key, _ in STEPS]
    if step_key == current:
        return "active"
    if order.index(step_key) < order.index(current):
        return "done"
    return "todo"
