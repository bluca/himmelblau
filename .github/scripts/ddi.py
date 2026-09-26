#!/usr/bin/env python3
"""Build, sign and publish stable/nightly DDIs without build-host."""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tarfile
import time

import ddi_image as image
import stable_packages as packages


SCRIPTS = Path(__file__).resolve().parent
NIGHTLY_REPOSITORY = "himmelblau/v_4"


def matrix(channel, tag="", revision="", distro="all", architecture="all", today=None):
    if channel == "stable":
        package_matrix = packages.matrix(tag, revision)
        first = json.loads(package_matrix["include"][0]["spec"])
        source_sha, tag_sha, repository = first["source_sha"], first["tag_sha"], first["repository"]
        label, upstream = tag, tag
    elif channel == "nightly":
        if tag or revision:
            raise ValueError("Nightly DDIs always use main, without a tag or revision override")
        source_ref = "refs/remotes/origin/main"
        if (os.environ.get("GITHUB_REPOSITORY") == "bluca/himmelblau"
                and os.environ.get("GITHUB_REF") == "refs/heads/ddi"
                and os.environ.get("GITHUB_EVENT_NAME") in ("push", "workflow_dispatch")):
            source_ref = os.environ["GITHUB_SHA"]
        source_sha = tag_sha = packages.resolve(source_ref)
        upstream = packages.version(source_sha)
        if not re.fullmatch(r"\d+\.\d+\.\d+", upstream):
            raise ValueError("Nightly source must have a MAJOR.MINOR.PATCH workspace version")
        repository = NIGHTLY_REPOSITORY
        date = today or dt.datetime.now(dt.timezone.utc).date().isoformat()
        label = f"{date}-{source_sha[:12]}"
        package_matrix = packages.source_matrix(upstream, tag_sha, source_sha, repository)
    else:
        raise ValueError("Channel must be stable or nightly")
    if architecture not in {"all", *packages.ARCHITECTURES}:
        raise ValueError("Architecture must be all, amd64 or arm64")

    dists, _ = packages.generator_config(packages.source_text(source_sha, "scripts/gen_dockerfiles.py"))
    makefile = packages.source_text(source_sha, "Makefile")
    targets = []
    for group in ("DEB", "RPM", "SLE", "GENTOO", "ARCH"):
        match = re.search(rf"^{group}_TARGETS\s*:=\s*(.+)$", makefile, re.MULTILINE)
        if match:
            targets.extend(match[1].split())
    if distro != "all" and distro not in targets:
        raise ValueError(f"Unsupported DDI distro: {distro}")
    fedora_targets = [t for t in targets if re.fullmatch(r"fedora\d+", t)]
    if not fedora_targets:
        raise ValueError("A supported, released Fedora target is required for portable DDIs")
    fedora = max(fedora_targets, key=lambda target: int(target.removeprefix("fedora")))
    native_packages = {
        (spec["distro"], spec["architecture"]): spec
        for entry in package_matrix["include"]
        for spec in [json.loads(entry["spec"])]
    }
    entries = {}
    for target in targets:
        if distro not in ("all", target):
            continue
        cfg = dists[target]
        for arch, info in packages.ARCHITECTURES.items():
            if architecture not in ("all", arch):
                continue
            package = native_packages.get((target, arch))
            if package is None and (cfg["family"] not in ("arch", "ebuild") or arch != "amd64"):
                continue
            entries[(target, arch)] = {
                "channel": channel, "version": label, "upstream": upstream,
                "source_sha": source_sha, "tag_sha": tag_sha, "repository": repository,
                "distro": target, "architecture": arch, "fedora": fedora,
                "scc": bool(cfg.get("scc")), "package": package,
                "kinds": ["sysext"], **info,
            }
    if not entries:
        raise ValueError("No supported DDI targets remain after applying the filters")
    for arch in {entry["architecture"] for entry in entries.values()}:
        package = native_packages.get((fedora, arch))
        if package is None:
            raise ValueError(f"The latest Fedora target does not support {arch}")
        entry = entries.setdefault((fedora, arch), {
            "channel": channel, "version": label, "upstream": upstream,
            "source_sha": source_sha, "tag_sha": tag_sha, "repository": repository,
            "distro": fedora, "architecture": arch, "fedora": fedora,
            "scc": False, "package": package, "kinds": [], **packages.ARCHITECTURES[arch],
        })
        entry["kinds"].append("portable")
    return {"include": [{"spec": json.dumps(entry, separators=(",", ":"))}
                        for _, entry in sorted(entries.items())]}


def prepare():
    result = matrix(
        os.environ["DDI_CHANNEL"], os.environ.get("RELEASE_TAG", ""),
        os.environ.get("REQUESTED_REVISION", ""),
        os.environ.get("REQUESTED_DISTRO") or "all",
        os.environ.get("REQUESTED_ARCHITECTURE") or "all",
    )
    packages.output("matrix", json.dumps(result, separators=(",", ":")))
    packages.output("tooling_sha", packages.resolve("HEAD"))
    packages.summary(f"Selected {len(result['include'])} DDI build targets.")


def expected_images(spec):
    arch = image.ARCHITECTURES[spec["architecture"]][0]
    result = []
    for kind in spec["kinds"]:
        if kind == "portable":
            filename = f"himmelblaud_{spec['version']}-{arch}.raw"
            name = f"himmelblau-{spec['channel']}-portable-{arch}"
        elif kind == "sysext":
            filename = f"himmelblau-{spec['distro']}_{spec['version']}-{arch}.sysext.raw"
            name = f"himmelblau-{spec['channel']}-{spec['distro']}-{arch}-sysext"
        else:
            raise ValueError(f"Invalid DDI kind: {kind}")
        result.append({"kind": kind, "filename": filename, "name": name, "version": spec["version"]})
    return result


def remote_plan(spec, expected_hashes=None):
    remotes = packages.api_packages(spec["repository"], "raw", spec["version"])
    missing, pending = [], False
    for record in expected_images(spec):
        matches = [
            remote for remote in remotes
            if remote.get("format") == "raw" and remote.get("name") == record["name"]
            and remote.get("version") == record["version"]
        ]
        if len(matches) > 1:
            raise ValueError(f"Duplicate Cloudsmith DDI: {record['name']}")
        if not matches:
            missing.append(record)
        elif matches[0].get("is_sync_failed"):
            raise ValueError(f"Cloudsmith DDI synchronization failed: {record['name']}")
        else:
            remote = matches[0]
            if remote.get("filename") != record["filename"]:
                raise ValueError(f"Cloudsmith DDI filename does not match: {record['name']}")
            if (remote.get("is_sync_completed") and expected_hashes
                    and record["name"] in expected_hashes
                    and remote.get("checksum_sha256") != expected_hashes[record["name"]]):
                raise ValueError(f"Cloudsmith DDI checksum does not match: {record['name']}")
            pending |= not matches[0].get("is_sync_completed", False)
    return missing, pending


def missing_images(spec, expected_hashes=None):
    packages.require_api_key()
    for attempt in range(30):
        missing, pending = remote_plan(spec, expected_hashes)
        if not pending:
            return missing
        if attempt == 29:
            raise RuntimeError("Cloudsmith DDIs did not synchronize within five minutes")
        time.sleep(10)


def preflight(spec):
    missing = missing_images(spec)
    packages.output("build_required", str(bool(missing)).lower())
    packages.summary(
        f"DDI preflight `{spec['channel']}/{spec['version']}` "
        f"`{spec['distro']}/{spec['architecture']}`: {len(missing)} missing images."
    )


def validate_files(directory, spec, *, signed):
    expected = {
        record["filename"] if signed else record["filename"] + ".tar"
        for record in expected_images(spec)
    }
    if not directory.is_dir() or {p.name for p in directory.iterdir()} != expected:
        raise ValueError("Incomplete or unexpected DDI artifact set")
    for name in expected:
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size < 4096:
            raise ValueError(f"Invalid DDI artifact: {name}")
        if signed:
            with path.open("rb") as stream:
                stream.seek(512)
                if stream.read(8) != b"EFI PART":
                    raise ValueError(f"Incorrect DDI artifact format: {name}")
        elif not tarfile.is_tarfile(path):
            raise ValueError(f"Incorrect DDI artifact format: {name}")


def build(source, artifacts, spec, *, container_cache_ref="", refresh_build_container=False):
    source, artifacts = source.resolve(), artifacts.resolve()
    if packages.run("git", "rev-parse", "HEAD", cwd=source) != spec["source_sha"]:
        raise ValueError("DDI checkout does not match resolved source")
    if packages.tomllib.loads((source / "Cargo.toml").read_text())["workspace"]["package"]["version"] != spec["upstream"]:
        raise ValueError("DDI source version does not match the resolved target")
    artifacts.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="ddi-build-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        work = Path(temporary)
        if spec["package"] is not None:
            package_dir = work / "packages"
            packages.build(source, package_dir, spec["package"],
                           container_cache_ref=container_cache_ref,
                           refresh_build_container=refresh_build_container)
            records = packages.validate_artifacts(package_dir, spec["package"])
            selected = {r["name"]: r["filename"] for r in records
                        if r["name"] in ("himmelblau", "pam-himmelblau", "nss-himmelblau")}
            if set(selected) != {"himmelblau", "pam-himmelblau", "nss-himmelblau"}:
                raise ValueError("DDIs require the daemon/CLI, PAM and NSS package set")
            release = image.release_fields(json.loads((package_dir / "manifest.json").read_text())["os_release"])
            if not release.get("VERSION_ID"):
                raise ValueError("The package distro must supply VERSION_ID")
            request = {"spec": spec, "packages": selected, "release": release}
            dockerfile = image.CONFIG / "Dockerfile"
        else:
            package_dir = work / "packages"
            package_dir.mkdir()
            request = {"spec": spec}
            dockerfile = image.CONFIG / f"Dockerfile.{spec['distro']}"
        (work / "request.json").write_text(json.dumps(request))
        builder = f"himmelblau-ddi-{spec['distro']}-{spec['architecture']}"
        # No source files (or credentials) enter the container build context.
        context = work / "context"
        context.mkdir()
        command = [
            "docker", "build", "--platform", spec["platform"],
            "--build-arg", f"FEDORA_RELEASE={spec['fedora'].removeprefix('fedora')}",
            "--build-arg", f"ARCH={spec['architecture']}",
            "--file", str(dockerfile), "--tag", builder,
        ]
        if refresh_build_container:
            command += ["--pull", "--no-cache"]
        image.run(command + [context])
        (source / "target").mkdir(exist_ok=True)
        image.run([
            "docker", "run", "--rm", "--platform", spec["platform"],
            "--security-opt", "label=disable",
            "--volume", f"{SCRIPTS}:/tooling:ro",
            "--volume", f"{source}:/source:ro",
            "--volume", f"{source / 'target'}:/source/target",
            "--volume", f"{package_dir}:/packages:ro",
            "--volume", f"{work / 'request.json'}:/request.json:ro",
            "--volume", f"{artifacts}:/output",
            builder, "python3", "/tooling/ddi.py", "stage", "--request", "/request.json",
        ])
    validate_files(artifacts, spec, signed=False)


def stage(request):
    spec = request["spec"]
    for record in expected_images(spec):
        root = Path("/") / ("root-" + record["kind"])
        if record["kind"] == "portable":
            image.stage_portable(
                {name: Path("/packages") / filename for name, filename in request["packages"].items()},
                root, spec["fedora"], spec["architecture"], spec["version"],
            )
        else:
            if spec["package"] is None:
                image.stage_native(Path("/source"), root, spec["architecture"])
                release = image.release_fields(Path("/etc/os-release").read_text())
                if release["ID"] != spec["distro"]:
                    raise ValueError("Native sysext was built on the wrong distribution")
            else:
                image.stage_sysext(
                    {name: Path("/packages") / filename for name, filename in request["packages"].items()},
                    root, spec["architecture"],
                )
                release = request["release"]
            image.extension_release(root, release, record["filename"], spec["version"], spec["architecture"])
        image.make_payload(root, Path("/output") / (record["filename"] + ".tar"))


def sign(artifacts, output, spec):
    artifacts, output = artifacts.resolve(), output.resolve()
    validate_files(artifacts, spec, signed=False)
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="ddi-payloads-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        roots = {}
        for record in expected_images(spec):
            root = Path(temporary) / record["kind"]
            image.extract_payload(artifacts / (record["filename"] + ".tar"), root)
            roots[record["kind"]] = root
        with image.signing_material(os.environ.get("RUNNER_TEMP")) as signing:
            for record in expected_images(spec):
                image.sign_image(roots[record["kind"]], output / record["filename"],
                                 spec["architecture"], signing, record["kind"])
    validate_files(output, spec, signed=True)


def publish(artifacts, spec):
    packages.require_api_key()
    validate_files(artifacts, spec, signed=True)
    uploaded = {}
    for record in missing_images(spec):
        tags = f"ddi,{record['kind']},channel-{spec['channel']},source-{spec['source_sha']},distro-{spec['distro']}"
        image.run([
            "cloudsmith", "push", "raw", spec["repository"], artifacts / record["filename"],
            "--name", record["name"], "--version", record["version"],
            "--content-type", "application/vnd.efi.img",
            "--no-republish", "--tags", tags,
        ], env=os.environ.copy())
        uploaded[record["name"]] = packages.digest(artifacts / record["filename"])
    if missing_images(spec, uploaded):
        raise RuntimeError("Cloudsmith verification did not find every signed DDI")
    packages.summary(f"Published raw-only DDIs for `{spec['channel']}/{spec['version']}` "
                     f"`{spec['distro']}/{spec['architecture']}` to `{spec['repository']}`.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "preflight", "build", "stage", "sign", "publish"))
    parser.add_argument("--source", type=Path, default=Path("source"))
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts"))
    parser.add_argument("--output", type=Path, default=Path("signed"))
    parser.add_argument("--request", type=Path)
    parser.add_argument("--container-cache-ref", default="")
    parser.add_argument("--refresh-build-container", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            prepare()
        elif args.command == "stage":
            stage(json.loads(args.request.read_text()))
        else:
            spec = json.loads(os.environ["TARGET_SPEC"])
            if args.command == "preflight":
                preflight(spec)
            elif args.command == "build":
                build(args.source, args.artifacts, spec, container_cache_ref=args.container_cache_ref,
                      refresh_build_container=args.refresh_build_container)
            elif args.command == "sign":
                sign(args.artifacts, args.output, spec)
            else:
                publish(args.artifacts, spec)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"DDI operation failed: {error}\n")


if __name__ == "__main__":
    main()
