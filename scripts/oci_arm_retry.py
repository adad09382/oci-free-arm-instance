#!/usr/bin/env python3
"""Retry OCI Always Free A1.Flex provisioning from a local machine."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NoReturn

import oci
import requests


CAPACITY_MARKERS = (
    "out of host capacity",
    "out of capacity",
    "capacity",
)

RETRYABLE_LOCAL_ERROR_MARKERS = (
    "connection aborted",
    "connection reset",
    "connection refused",
    "max retries exceeded",
    "nameresolutionerror",
    "protocolerror",
    "read timed out",
    "remotedisconnected",
    "temporary failure",
    "timeout",
)


@dataclass(frozen=True)
class Settings:
    tenancy: str
    user: str
    region: str
    fingerprint: str
    key_file: str | None
    key_content: str | None
    compartment_id: str
    subnet_id: str
    availability_domain: str
    image_id: str
    ssh_public_key: str
    discord_webhook_url: str | None
    instance_display_name: str
    availability_domains: tuple[str, ...]
    shape_configs: tuple[tuple[float, float], ...]
    ocpus: float
    memory_gbs: float
    boot_volume_gbs: int
    retry_interval_seconds: int
    discord_capacity_every: int
    launchd_label: str | None


def log(message: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def fail(message: str) -> NoReturn:
    log(f"ERROR: {message}")
    raise SystemExit(1)


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values

    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if value.startswith("-----BEGIN ") and "PRIVATE KEY-----" in value and "-----END " not in value:
            parts = [value]
            while i < len(lines):
                parts.append(lines[i])
                if "-----END " in lines[i]:
                    i += 1
                    break
                i += 1
            value = "\n".join(parts)
        elif value.startswith(("'", '"')):
            quote = value[0]
            value = value[1:]
            parts: list[str] = []
            while True:
                if value.endswith(quote) and not value.endswith(f"\\{quote}"):
                    parts.append(value[:-1])
                    break
                parts.append(value)
                if i >= len(lines):
                    break
                value = lines[i]
                i += 1
            value = "\n".join(parts)
        else:
            value = value.split(" #", 1)[0].strip()

        values[key] = value.encode("utf-8").decode("unicode_escape")

    return values


def merged_env(env_file: Path | None) -> dict[str, str]:
    values = dict(os.environ)
    if env_file:
        file_values = parse_env_file(env_file.expanduser())
        values.update({key: val for key, val in file_values.items() if val != ""})
    return values


def required(values: dict[str, str], key: str) -> str:
    value = values.get(key, "").strip()
    if not value:
        fail(f"Missing required setting: {key}")
    return value


def split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def parse_shape_configs(value: str) -> tuple[tuple[float, float], ...]:
    configs: list[tuple[float, float]] = []
    for item in split_csv(value):
        if ":" not in item:
            fail(f"Invalid SHAPE_CONFIGS item, expected OCPU:MEMORY_GB: {item}")
        ocpus, memory = item.split(":", 1)
        configs.append((float(ocpus.strip()), float(memory.strip())))
    return tuple(configs)


def load_settings(args: argparse.Namespace) -> Settings:
    values = merged_env(args.env_file)
    key_file = values.get("OCI_CLI_KEY_FILE", "").strip() or None
    key_content = values.get("OCI_CLI_KEY_CONTENT", "").strip() or None

    if key_file:
        key_file = str(Path(key_file).expanduser())
    if not key_file and not key_content:
        fail("Set OCI_CLI_KEY_FILE or OCI_CLI_KEY_CONTENT")
    if key_file and not Path(key_file).exists():
        fail(f"OCI_CLI_KEY_FILE does not exist: {key_file}")
    if key_content:
        key_content = normalize_private_key(key_content)

    availability_domains = split_csv(values.get("AD_NAMES", "")) or (required(values, "AD_NAME"),)
    ocpus = float(values.get("OCPUS", args.ocpus))
    memory_gbs = float(values.get("MEMORY_GBS", args.memory_gbs))
    shape_configs = parse_shape_configs(values.get("SHAPE_CONFIGS", "")) or ((ocpus, memory_gbs),)

    return Settings(
        tenancy=required(values, "OCI_CLI_TENANCY"),
        user=required(values, "OCI_CLI_USER"),
        region=required(values, "OCI_CLI_REGION"),
        fingerprint=required(values, "OCI_CLI_FINGERPRINT"),
        key_file=key_file,
        key_content=key_content,
        compartment_id=required(values, "OCI_COMPARTMENT_ID"),
        subnet_id=required(values, "OCI_SUBNET_ID"),
        availability_domain=availability_domains[0],
        image_id=required(values, "IMAGE_ID"),
        ssh_public_key=required(values, "SSH_PUBLIC_KEY"),
        discord_webhook_url=values.get("DISCORD_WEBHOOK_URL", "").strip() or None,
        instance_display_name=values.get("INSTANCE_DISPLAY_NAME", "free-arm-instance"),
        availability_domains=availability_domains,
        shape_configs=shape_configs,
        ocpus=shape_configs[0][0],
        memory_gbs=shape_configs[0][1],
        boot_volume_gbs=int(values.get("BOOT_VOLUME_GBS", args.boot_volume_gbs)),
        retry_interval_seconds=int(values.get("RETRY_INTERVAL_SECONDS", args.interval)),
        discord_capacity_every=int(values.get("DISCORD_CAPACITY_EVERY", "0")),
        launchd_label=values.get("LAUNCHD_LABEL", "com.wade.oci-arm-retry").strip() or None,
    )


def normalize_private_key(value: str) -> str:
    value = value.strip().strip("\"'")
    value = value.replace("\\r\\n", "\n").replace("\\n", "\n")
    value = value.replace("-----BEGIN PRIVATE KEY----- ", "-----BEGIN PRIVATE KEY-----\n")
    value = value.replace(" -----END PRIVATE KEY-----", "\n-----END PRIVATE KEY-----")
    return value.strip() + "\n"


def make_compute_client(settings: Settings) -> oci.core.ComputeClient:
    config = {
        "region": settings.region,
        "tenancy": settings.tenancy,
        "user": settings.user,
        "fingerprint": settings.fingerprint,
        "key_file": settings.key_file or "__private_key_content__",
    }
    if settings.key_content:
        signer = oci.signer.Signer(
            tenancy=settings.tenancy,
            user=settings.user,
            fingerprint=settings.fingerprint,
            private_key_file_location=None,
            private_key_content=settings.key_content,
        )
    else:
        signer = oci.signer.Signer(
            tenancy=settings.tenancy,
            user=settings.user,
            fingerprint=settings.fingerprint,
            private_key_file_location=settings.key_file,
        )
    return oci.core.ComputeClient(config, signer=signer)


def launch_instance(
    client: oci.core.ComputeClient,
    settings: Settings,
    availability_domain: str,
    ocpus: float,
    memory_gbs: float,
) -> oci.response.Response:
    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=availability_domain,
        compartment_id=settings.compartment_id,
        display_name=settings.instance_display_name,
        shape="VM.Standard.A1.Flex",
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=ocpus,
            memory_in_gbs=memory_gbs,
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=settings.subnet_id,
            assign_public_ip=True,
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=settings.image_id,
            boot_volume_size_in_gbs=settings.boot_volume_gbs,
        ),
        metadata={"ssh_authorized_keys": settings.ssh_public_key},
    )
    return client.launch_instance(details)


def send_discord(settings: Settings, title: str, description: str, color: int) -> None:
    if not settings.discord_webhook_url:
        return

    payload = {"embeds": [{"title": title, "description": description[:3900], "color": color}]}
    try:
        response = requests.post(settings.discord_webhook_url, json=payload, timeout=15)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log(f"Discord notification failed: {exc}")


def is_capacity_error(exc: oci.exceptions.ServiceError) -> bool:
    haystack = f"{exc.code} {exc.message}".lower()
    return any(marker in haystack for marker in CAPACITY_MARKERS)


def format_service_error(exc: oci.exceptions.ServiceError) -> str:
    data = {
        "status": exc.status,
        "code": exc.code,
        "message": exc.message,
        "opc-request-id": exc.request_id,
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


def is_retryable_local_error(exc: Exception) -> bool:
    haystack = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in haystack for marker in RETRYABLE_LOCAL_ERROR_MARKERS)


def send_retry_heartbeat(settings: Settings, attempts: int, batch_results: list[str]) -> None:
    if settings.discord_capacity_every <= 0 or attempts % settings.discord_capacity_every != 0:
        return
    description = f"Attempts: {attempts}\n\n" + "\n".join(batch_results[-settings.discord_capacity_every :])
    send_discord(
        settings,
        "⏳ OCI retry heartbeat",
        description,
        15105570,
    )
    batch_results.clear()


def unload_launchd(label: str | None) -> None:
    if not label:
        return
    try:
        subprocess.run(["launchctl", "remove", label], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log(f"Requested launchd stop: {label}")
    except Exception as exc:  # noqa: BLE001
        log(f"launchd stop failed: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Retry OCI A1.Flex instance creation locally.")
    parser.add_argument("--env-file", type=Path, default=Path("oci-secrets.env"))
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--ocpus", type=float, default=2)
    parser.add_argument("--memory-gbs", type=float, default=12)
    parser.add_argument("--boot-volume-gbs", type=int, default=50)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--validate-only", action="store_true", help="Load config and initialize the OCI client without launching.")
    args = parser.parse_args()

    settings = load_settings(args)
    client = make_compute_client(settings)
    if args.validate_only:
        log(
            "Config OK "
            f"region={settings.region} ads={','.join(settings.availability_domains)} "
            f"shapes={','.join(f'{ocpus:g}/{memory:g}' for ocpus, memory in settings.shape_configs)}"
        )
        return 0

    stopping = False

    def handle_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    log(
        "Starting OCI ARM retry "
        f"shape=VM.Standard.A1.Flex "
        f"shape_configs={','.join(f'{ocpus:g}/{memory:g}' for ocpus, memory in settings.shape_configs)} "
        f"interval={settings.retry_interval_seconds}s "
        f"ads={','.join(settings.availability_domains)}"
    )

    attempts = 0
    batch_results: list[str] = []
    while not stopping:
        attempts += 1
        availability_domain = settings.availability_domains[(attempts - 1) % len(settings.availability_domains)]
        ocpus, memory_gbs = settings.shape_configs[(attempts - 1) % len(settings.shape_configs)]
        try:
            response = launch_instance(client, settings, availability_domain, ocpus, memory_gbs)
            instance = response.data
            message = (
                f"Instance created: {instance.display_name}\n"
                f"OCID: {instance.id}\n"
                f"Lifecycle state: {instance.lifecycle_state}\n"
                f"Region: {settings.region}\n"
                f"Availability domain: {availability_domain}\n"
                f"Shape config: {ocpus:g} OCPU / {memory_gbs:g}GB"
            )
            log(message.replace("\n", " | "))
            send_discord(settings, "✅ OCI VM Created!", message, 3066993)
            unload_launchd(settings.launchd_label)
            return 0
        except oci.exceptions.ServiceError as exc:
            if is_capacity_error(exc):
                result = f"#{attempts}: capacity {ocpus:g}/{memory_gbs:g} ad={availability_domain} ({exc.request_id})"
                batch_results.append(result)
                log(
                    "Out of capacity, retrying... "
                    f"attempt={attempts} shape={ocpus:g}/{memory_gbs:g} "
                    f"ad={availability_domain} request_id={exc.request_id}"
                )
                send_retry_heartbeat(settings, attempts, batch_results)
            else:
                detail = format_service_error(exc)
                log(f"Unexpected OCI error: {detail}")
                send_discord(settings, "❌ Unexpected OCI Error", detail, 15158332)
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            if is_retryable_local_error(exc):
                batch_results.append(f"#{attempts}: network retry {ocpus:g}/{memory_gbs:g} ad={availability_domain} ({detail[:220]})")
                log(
                    "Retryable local error, retrying... "
                    f"attempt={attempts} shape={ocpus:g}/{memory_gbs:g} "
                    f"ad={availability_domain} detail={detail}"
                )
                send_retry_heartbeat(settings, attempts, batch_results)
            else:
                log(f"Unexpected local error: {detail}")
                send_discord(settings, "❌ Unexpected Local Error", detail, 15158332)

        if args.once:
            return 1
        time.sleep(settings.retry_interval_seconds)

    log("Stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
