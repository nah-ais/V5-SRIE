"""
ui/components.py
-----------------
Komponen UI generik & reusable (styling, header halaman, progress bar
langkah wizard, kartu ringkasan metrik). Tidak ada logika data di sini —
murni presentasi, supaya mudah dipakai ulang di setiap langkah wizard.
"""

from __future__ import annotations

import streamlit as st


def inject_css() -> None:
    """Suntikkan CSS ringan untuk mempercantik tampilan (kartu, badge, dsb)."""
    st.markdown(
        """
        <style>
        .block-container {max-width: 1450px; padding-top: 1.5rem; padding-bottom: 3rem;}

        .app-title {font-size: 2rem; font-weight: 800; margin-bottom: .15rem;}
        .app-subtitle {color: #6b7280; margin-bottom: 1.2rem;}

        .workflow-card {
            border: 1px solid #e5e7eb; border-radius: 14px; padding: 14px 16px;
            background: #ffffff; min-height: 92px;
        }
        .workflow-active {border: 2px solid #2563eb; background: #eff6ff;}
        .workflow-done {border: 1px solid #86efac; background: #f0fdf4;}

        .step-number {font-weight: 800; font-size: 1.05rem;}
        .step-label {font-weight: 700; margin-top: 5px;}
        .step-state {font-size: .8rem; color: #6b7280; margin-top: 4px;}

        .summary-card {
            border: 1px solid #e5e7eb; border-radius: 12px; padding: 14px;
            background: #fafafa; text-align: center;
        }
        .summary-number {font-size: 1.7rem; font-weight: 800;}
        .summary-label {font-size: .85rem; color: #6b7280;}

        .decision-card {
            border: 1px solid #e5e7eb; border-radius: 14px; padding: 18px;
            background: #fff;
        }
        .score-box {
            border-radius: 12px; padding: 14px; background: #f8fafc;
            border: 1px solid #e2e8f0; margin-bottom: 12px;
        }
        .small-note {color: #6b7280; font-size: .9rem;}
        </style>
        """,
        unsafe_allow_html=True,
    )


def page_header() -> None:
    """Judul & subjudul halaman utama."""
    st.markdown('<div class="app-title">🧹 SRIE Data Cleaning System</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="app-subtitle">Ikuti langkah dari kiri ke kanan. Sistem akan membantu panitia '
        'membersihkan data tanpa perlu memahami proses teknis di belakangnya.</div>',
        unsafe_allow_html=True,
    )


def workflow_bar(steps: list[tuple[str, str]], current_step: str, status_func) -> None:
    """
    Render progress bar horizontal berisi kartu untuk setiap langkah wizard,
    dengan gaya visual berbeda untuk status done/active/todo.

    Parameters
    ----------
    steps : list of (key, label)
        Daftar langkah, biasanya `STEPS` dari workflow_service.
    current_step : str
        Key langkah yang sedang aktif (tidak dipakai langsung di sini,
        status tiap kartu ditentukan lewat `status_func`).
    status_func : callable(step_key) -> "done" | "active" | "todo"
    """
    status_labels = {"done": "✓ Selesai", "active": "Sedang dikerjakan", "todo": "Menunggu"}
    cols = st.columns(len(steps))

    for col, (key, label) in zip(cols, steps):
        state = status_func(key)
        css_class = "workflow-card"
        if state == "active":
            css_class += " workflow-active"
        elif state == "done":
            css_class += " workflow-done"

        step_number, step_label = label.split(" ", 1)
        with col:
            st.markdown(
                f'<div class="{css_class}">'
                f'<div class="step-number">{step_number}</div>'
                f'<div class="step-label">{step_label}</div>'
                f'<div class="step-state">{status_labels[state]}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )


def metric_row(items: list[tuple[str, object]]) -> None:
    """Render beberapa st.metric berjajar dalam kolom, dari list (label, value)."""
    cols = st.columns(len(items))
    for col, (label, value) in zip(cols, items):
        with col:
            st.metric(label, value)


def empty_state(title: str, message: str) -> None:
    """Kotak info standar untuk kondisi "belum ada data" di suatu langkah."""
    st.info(f"**{title}**\n\n{message}")


def confirmation_summary(title: str, lines: list[str]) -> None:
    """Kotak sukses dengan daftar checklist ringkas di bawahnya."""
    st.success(title)
    for line in lines:
        st.write(f"✓ {line}")
