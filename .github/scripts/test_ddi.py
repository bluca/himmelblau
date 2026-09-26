"""DDI contracts: no containers, live credentials or publication operations."""

import base64
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import shutil
import signal
import struct
import subprocess
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import ddi
import ddi_image as image
import stable_packages as packages


MANPAGES = (
    "man1/aad-tool.1", "man5/himmelblau.conf.5",
    "man8/himmelblaud.8", "man8/himmelblaud_tasks.8", "man8/pam_himmelblau.8",
)


def write(root, name, content=b"fixture\n"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def elf(architecture="amd64"):
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    header[18:20] = image.ARCHITECTURES[architecture][1].to_bytes(2, "little")
    return header


def spec(architecture="amd64", distro="fedora44", channel="stable", kinds=None):
    return {
        "channel": channel, "version": "4.1.0" if channel == "stable" else "2026-09-26-" + "a" * 12,
        "upstream": "4.1.0", "source_sha": "a" * 40, "tag_sha": "b" * 40,
        "repository": "himmelblau/v_4", "distro": distro, "fedora": "fedora44",
        "architecture": architecture, "scc": False, "package": None,
        "kinds": kinds or ["sysext", "portable"], **packages.ARCHITECTURES[architecture],
    }


def artifact(directory, record, signed):
    data = bytearray(8192)
    if signed:
        data[512:520] = b"EFI PART"
        return write(directory, record["filename"], data)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (record["filename"] + ".tar")
    with tarfile.open(path, "w") as archive:
        member = tarfile.TarInfo("usr/payload")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    return path


class MatrixTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "debian13": {"family": "deb"},
            "rocky8": {"family": "rpm", "arm64": False},
            "fedora43": {"family": "rpm"}, "fedora44": {"family": "rpm"},
            "rawhide": {"family": "rpm"}, "sle16": {"family": "zypper"},
            "arch": {"family": "arch"}, "gentoo": {"family": "ebuild"},
        }
        self.manifest = (
            "DEB_TARGETS := debian13\nRPM_TARGETS := rocky8 fedora43 fedora44 rawhide\n"
            "SLE_TARGETS := sle16\nARCH_TARGETS := arch\nGENTOO_TARGETS := gentoo\n"
        )

        def source_text(commit, path):
            if path == "Makefile":
                return self.manifest
            if path == "scripts/gen_dockerfiles.py":
                return (
                    f"DISTS = {self.config!r}\n"
                    'PACKAGES = [("himmelblaud", "src/daemon", True)]\n'
                    'raise RuntimeError("must not execute source configuration")\n'
                )
            if path == "Cargo.toml":
                return '[workspace.package]\nversion="4.1.0"\n'
            return (
                '[package]\nname="himmelblaud"\n[package.metadata.deb]\nname="himmelblau"\n'
                '[package.metadata.generate-rpm]\nname="himmelblau"\n'
            )

        for name, value in (("source_text", source_text), ("resolve", lambda _: "a" * 40),
                            ("ancestor", lambda *_: True)):
            mock = patch.object(packages, name, side_effect=value)
            mock.start()
            self.addCleanup(mock.stop)

    def entries(self, channel="stable", **kwargs):
        result = ddi.matrix(channel, tag="4.1.0" if channel == "stable" else "", **kwargs)
        return [json.loads(entry["spec"]) for entry in result["include"]]

    def test_stable_and_nightly_cover_every_supported_distribution(self):
        for channel in ("stable", "nightly"):
            with self.subTest(channel=channel):
                entries = self.entries(channel, today="2026-09-26")
                self.assertEqual({entry["distro"] for entry in entries}, set(self.config))
                self.assertEqual(len(entries), 13)
                portable = [entry for entry in entries if "portable" in entry["kinds"]]
                self.assertEqual({(e["distro"], e["architecture"]) for e in portable},
                                 {("fedora44", "amd64"), ("fedora44", "arm64")})
                self.assertTrue(all(e["repository"] == "himmelblau/v_4" for e in entries))
                if channel == "nightly":
                    self.assertTrue(all(e["version"] == "2026-09-26-" + "a" * 12 for e in entries))
                self.assertFalse(any(e["architecture"] == "arm64" and e["distro"] in ("rocky8", "arch", "gentoo")
                                     for e in entries))

    def test_filtered_distro_keeps_one_global_portable_per_architecture(self):
        entries = self.entries(distro="debian13", architecture="arm64")
        self.assertEqual([(e["distro"], e["kinds"]) for e in entries],
                         [("debian13", ["sysext"]), ("fedora44", ["portable"])])
        self.assertTrue(all(e["runner"] == "ubuntu-24.04-arm" for e in entries))

    def test_main_version_five_nightlies_publish_to_the_requested_v4_repository(self):
        with patch.object(packages, "version", return_value="5.0.0"):
            entries = self.entries("nightly", today="2026-09-26")
        self.assertTrue(all(e["repository"] == "himmelblau/v_4" and e["upstream"] == "5.0.0" for e in entries))

    def test_fork_ddi_runs_build_the_triggering_commit_not_main(self):
        commit = "c" * 40
        for event in ("push", "workflow_dispatch"):
            with self.subTest(event=event), patch.dict(os.environ, {
                "GITHUB_REPOSITORY": "bluca/himmelblau", "GITHUB_REF": "refs/heads/ddi",
                "GITHUB_EVENT_NAME": event, "GITHUB_SHA": commit,
            }), patch.object(packages, "resolve", return_value=commit) as resolve:
                entries = self.entries("nightly", today="2026-09-26")
            resolve.assert_called_once_with(commit)
            self.assertTrue(all(e["source_sha"] == commit for e in entries))
            self.assertTrue(all(e["version"] == "2026-09-26-" + commit[:12] for e in entries))
            self.assertTrue(all(e["package"] is None or e["package"]["source_sha"] == commit for e in entries))

    def test_nightly_source_override_is_limited_to_the_fork_test_branch(self):
        for repository, ref, event in (
            ("himmelblau-idm/himmelblau", "refs/heads/ddi", "push"),
            ("bluca/himmelblau", "refs/heads/main", "workflow_dispatch"),
            ("bluca/himmelblau", "refs/heads/ddi", "schedule"),
        ):
            with self.subTest(repository=repository, ref=ref, event=event), patch.dict(os.environ, {
                "GITHUB_REPOSITORY": repository, "GITHUB_REF": ref,
                "GITHUB_EVENT_NAME": event, "GITHUB_SHA": "c" * 40,
            }), patch.object(packages, "resolve", return_value="a" * 40) as resolve:
                self.entries("nightly", today="2026-09-26")
            resolve.assert_called_once_with("refs/remotes/origin/main")

    def test_native_distro_can_be_selected_without_a_native_cloudsmith_package(self):
        entries = self.entries(distro="gentoo")
        gentoo = next(e for e in entries if e["distro"] == "gentoo")
        self.assertIsNone(gentoo["package"])
        self.assertEqual(gentoo["kinds"], ["sysext"])

    def test_invalid_or_untrusted_selection_fails(self):
        for kwargs in ({"tag": "4.1.0;bad"}, {"tag": "4.1.0", "distro": "unknown"},
                       {"tag": "4.1.0", "architecture": "mips"},
                       {"tag": "4.1.0", "distro": "rocky8", "architecture": "arm64"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ddi.matrix("stable", **kwargs)
        for kwargs in ({"tag": "4.1.0"}, {"revision": "untrusted"}):
            with self.assertRaises(ValueError):
                ddi.matrix("nightly", **kwargs)
        with patch.object(packages, "ancestor", return_value=False), self.assertRaises(ValueError):
            ddi.matrix("stable", "4.1.0")


class RepositoryMatrixTests(unittest.TestCase):
    def test_current_main_build_configuration_is_fully_covered(self):
        root = Path(__file__).resolve().parents[2]
        with patch.object(packages, "source_text", side_effect=lambda _, path: (root / path).read_text()), \
                patch.object(packages, "resolve", return_value="a" * 40):
            result = ddi.matrix("nightly", today="2026-09-26")
        entries = [json.loads(entry["spec"]) for entry in result["include"]]
        config, _ = packages.generator_config((root / "scripts/gen_dockerfiles.py").read_text())
        self.assertEqual({entry["distro"] for entry in entries}, set(config) - {"test"})
        self.assertTrue(all(entry["repository"] == "himmelblau/v_4" for entry in entries))
        self.assertEqual(len([entry for entry in entries if "portable" in entry["kinds"]]), 2)


class CloudsmithTests(unittest.TestCase):
    def setUp(self):
        self.spec = spec()
        self.records = ddi.expected_images(self.spec)

    def remote(self, record, **updates):
        return {"format": "raw", "name": record["name"], "version": record["version"],
                "filename": record["filename"], "is_sync_completed": True, **updates}

    def test_complete_images_skip_even_when_native_packages_are_missing(self):
        with patch.dict(os.environ, {"CLOUDSMITH_API_KEY": "test-only"}), \
                patch.object(packages, "api_packages", return_value=[self.remote(r) for r in self.records]) as lookup, \
                patch.object(packages, "output") as output:
            ddi.preflight(self.spec)
        lookup.assert_called_once_with("himmelblau/v_4", "raw", "4.1.0")
        output.assert_called_once_with("build_required", "false")

    def test_missing_image_is_not_hidden_by_a_package_or_wrong_version(self):
        for updates in ({"format": "rpm"}, {"version": "4.0.0"}, {"name": "different"}):
            with self.subTest(updates=updates), patch.object(
                packages, "api_packages", return_value=[self.remote(self.records[0], **updates)],
            ):
                self.assertEqual(len(ddi.remote_plan(self.spec)[0]), 2)
        with patch.object(packages, "api_packages", return_value=[self.remote(self.records[0])]):
            self.assertEqual(ddi.remote_plan(self.spec), ([self.records[1]], False))

    def test_duplicate_and_failed_images_are_explicit_errors(self):
        for remotes in ([self.remote(self.records[0])] * 2,
                        [self.remote(self.records[0], is_sync_failed=True)]):
            with patch.object(packages, "api_packages", return_value=remotes), self.assertRaises(ValueError):
                ddi.remote_plan(self.spec)

    def test_published_checksums_are_verified(self):
        with patch.object(packages, "api_packages", return_value=[
            self.remote(record, checksum_sha256="a" * 64) for record in self.records
        ]):
            self.assertEqual(ddi.remote_plan(self.spec, {self.records[0]["name"]: "a" * 64}), ([], False))
            with self.assertRaisesRegex(ValueError, "checksum"):
                ddi.remote_plan(self.spec, {self.records[0]["name"]: "b" * 64})

    def test_pending_images_are_awaited(self):
        ready = [self.remote(r) for r in self.records]
        with patch.dict(os.environ, {"CLOUDSMITH_API_KEY": "test-only"}), \
                patch.object(packages, "api_packages", side_effect=[
                    [self.remote(self.records[0], is_sync_completed=False)], ready,
                ]), patch.object(ddi.time, "sleep") as sleep:
            self.assertEqual(ddi.missing_images(self.spec), [])
            sleep.assert_called_once_with(10)

    def test_raw_query_uses_exact_version_not_deb_rpm_revision_pattern(self):
        with patch.dict(os.environ, {"CLOUDSMITH_API_KEY": "test-only"}), \
                patch.object(packages.urllib.request, "urlopen", return_value=io.BytesIO(b"[]")) as request:
            packages.api_packages("himmelblau/v_4", "raw", "4.1.0")
        self.assertIn("version%3A4.1.0", request.call_args.args[0].full_url)
        self.assertNotIn("4.1.0-%2A", request.call_args.args[0].full_url)

    def test_only_raw_files_are_published_and_existing_images_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifacts = Path(temporary)
            for record in self.records:
                artifact(artifacts, record, signed=True)
            with patch.dict(os.environ, {"CLOUDSMITH_API_KEY": "test-only"}), \
                    patch.object(ddi, "missing_images", side_effect=([self.records[1]], [])), \
                    patch.object(image, "run") as run:
                ddi.publish(artifacts, self.spec)
            command = run.call_args.args[0]
            self.assertEqual(command[:4], ["cloudsmith", "push", "raw", "himmelblau/v_4"])
            self.assertEqual(command[4], artifacts / self.records[1]["filename"])
            self.assertIn("--no-republish", command)
            self.assertNotIn("test-only", " ".join(map(str, command)))
            self.assertEqual(run.call_count, 1)
            write(artifacts, "certificate.crt")
            with self.assertRaises(ValueError):
                ddi.validate_files(artifacts, self.spec, signed=True)

    def test_credentials_are_required_before_publication(self):
        with patch.dict(os.environ, {"CLOUDSMITH_API_KEY": ""}), \
                patch.object(image, "run") as run, self.assertRaises(ValueError):
            ddi.publish(Path("/unused"), self.spec)
        run.assert_not_called()

    def test_publication_checks_the_uploaded_file_hash_without_a_sidecar(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for record in self.records:
                artifact(directory, record, signed=True)
            remotes = [
                self.remote(record, checksum_sha256=packages.digest(directory / record["filename"]))
                for record in self.records
            ]
            with patch.dict(os.environ, {"CLOUDSMITH_API_KEY": "test-only"}), \
                    patch.object(packages, "api_packages", side_effect=[[], remotes]), \
                    patch.object(image, "run") as upload:
                ddi.publish(directory, self.spec)
            self.assertEqual(upload.call_count, 2)
            self.assertEqual({path.suffix for path in directory.iterdir()}, {".raw"})


class PayloadTests(unittest.TestCase):
    def test_sysext_contains_host_components_but_no_distro_libraries_or_daemons(self):
        for architecture, library in (("amd64", "usr/lib/x86_64-linux-gnu"),
                                      ("arm64", "usr/lib/aarch64-linux-gnu")):
            with self.subTest(architecture=architecture), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                core = work / "packages/himmelblau"
                write(core, "usr/bin/aad-tool", elf(architecture))
                write(core, "usr/sbin/himmelblaud", elf(architecture))
                write(core, "usr/lib/libssl.so.3", elf(architecture))
                write(core, "usr/lib/systemd/system/himmelblaud.service")
                write(core, "usr/lib/systemd/user/himmelblau-compliance-check.service")
                write(core, "usr/lib/systemd/user/himmelblau-compliance-check.timer")
                write(core, "usr/lib/himmelblau/himmelblau.conf")
                write(core, "usr/lib/tmpfiles.d/himmelblaud.conf")
                write(core, "usr/lib/os-release")
                write(core, "usr/share/doc/himmelblau/README")
                suffix = ".gz" if architecture == "amd64" else ""
                for name in MANPAGES[:-1]:
                    write(core, "usr/share/man/" + name + suffix, name.encode())
                write(work / "packages/pam-himmelblau", f"{library}/security/pam_himmelblau.so", elf(architecture))
                write(work / "packages/pam-himmelblau", "usr/share/man/" + MANPAGES[-1] + suffix,
                      MANPAGES[-1].encode())
                write(work / "packages/nss-himmelblau", f"{library}/libnss_himmelblau.so.2", elf(architecture))

                def extract(package, destination):
                    shutil.copytree(work / "packages" / package.name, destination)

                root = work / "root"
                with patch.object(image, "extract_package", side_effect=extract):
                    image.stage_sysext(
                        {name: Path(name) for name in ("himmelblau", "pam-himmelblau", "nss-himmelblau")},
                        root, architecture,
                    )
                self.assertTrue((root / f"{library}/security/pam_himmelblau.so").is_file())
                for name in MANPAGES:
                    self.assertEqual((root / "usr/share/man" / (name + suffix)).read_bytes(), name.encode())
                for absent in ("usr/sbin", "usr/lib/libssl.so.3", "usr/lib/os-release",
                               "usr/lib/systemd/system", "usr/lib/systemd/user", "usr/lib/systemd/portable"):
                    self.assertFalse((root / absent).exists(), absent)

    def test_sysext_rejects_units_from_host_component_packages(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"

            def extract(package, destination):
                write(destination, "usr/lib/systemd/user/unexpected.service")

            with patch.object(image, "extract_package", side_effect=extract), \
                    self.assertRaisesRegex(ValueError, "must not contain systemd units"):
                image.stage_sysext({"pam-himmelblau": Path("pam.rpm")}, root, "amd64")

    def test_release_metadata_filename_uses_unversioned_image_name(self):
        for release in ({"ID": "debian", "VERSION_ID": "13"},
                        {"ID": "arch"}, {"ID": "gentoo", "VERSION_ID": "2.18"}):
            for filename, version in (
                ("himmelblau-test_4.1.0-arm64.sysext.raw", "4.1.0"),
                ("himmelblau-test_2026-09-27-abcdef123456-arm64.sysext.raw", "2026-09-27-abcdef123456"),
                ("himmelblau-test_4.1.0_extra-arm64.sysext.raw", "4.1.0_extra"),
                ("himmelblau-test.sysext.raw", "4.1.0"),
            ):
                with self.subTest(release=release, filename=filename), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    image.extension_release(root, release, filename, version, "arm64")
                    directory = root / "usr/lib/extension-release.d"
                    self.assertEqual([path.name for path in directory.iterdir()], ["extension-release.himmelblau-test"])
                    metadata = image.release_fields((directory / "extension-release.himmelblau-test").read_text())
                    self.assertEqual(metadata["ID"], release["ID"])
                    self.assertEqual(metadata.get("VERSION_ID"), release.get("VERSION_ID"))
                    self.assertEqual(metadata["ARCHITECTURE"], "arm64")
                    self.assertEqual(metadata["SYSEXT_SCOPE"], "system")
                    self.assertEqual(metadata["SYSEXT_VERSION_ID"], version)

    def test_portable_keeps_existing_units_and_has_no_custom_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            units = (
                "usr/lib/systemd/system/himmelblaud.service",
                "usr/lib/systemd/system/himmelblaud-tasks.service",
                "usr/lib/systemd/system/himmelblau-hsm-pin-init.service",
                "usr/lib/systemd/user/himmelblau-compliance-check.service",
                "usr/lib/systemd/user/himmelblau-compliance-check.timer",
            )

            def install(command):
                self.assertEqual(command[0], "dnf")
                self.assertIn("--use-host-config", command)
                self.assertIn("--setopt=install_weak_deps=False", command)
                self.assertIn("--setopt=tsflags=nodocs", command)
                if command[-2:] == ["clean", "all"]:
                    return
                requested = command[command.index("install") + 1:]
                self.assertEqual(set(requested), {
                    Path("himmelblau.rpm"), Path("nss.rpm"),
                    "filesystem", "ca-certificates", "bash", "coreutils",
                    "glibc-common", "grep", "shadow-utils", "util-linux-core",
                    "systemd", "tpm2-tools",
                })
                for name in ("himmelblaud", "himmelblaud_tasks"):
                    write(root, "usr/sbin/" + name, elf())
                write(root, "usr/bin/aad-tool", elf())
                write(root, "usr/bin/systemd-creds", elf())
                write(root, "usr/bin/systemd-analyze", elf())
                write(root, "usr/lib/os-release", b"ID=fedora\nVERSION_ID=44\n")
                write(root, "etc/machine-id", b"must-not-ship")
                for name in units:
                    write(root, name, name.encode())

            with patch.object(image, "run", side_effect=install) as run:
                image.stage_portable({"himmelblau": Path("himmelblau.rpm"), "nss-himmelblau": Path("nss.rpm")},
                                     root, "fedora44", "amd64", "4.1.0")
            self.assertEqual(run.call_count, 2)
            self.assertEqual(run.call_args.args[0][-2:], ["clean", "all"])
            for name in units:
                self.assertEqual((root / name).read_text(), name)
            for name in ("aad-tool", "systemd-creds", "systemd-analyze"):
                self.assertTrue((root / "usr/bin" / name).is_file())
            self.assertFalse((root / "usr/share/man").exists())
            self.assertFalse((root / "usr/lib/systemd/portable").exists())
            self.assertEqual((root / "etc/machine-id").read_bytes(), b"")
            self.assertIn('PORTABLE_PREFIXES="himmelblaud himmelblau-hsm-pin-init"',
                          (root / "usr/lib/os-release").read_text())
            mountpoints = ("var/lib/himmelblaud", "var/cache/himmelblaud")
            for relative in mountpoints:
                self.assertTrue((root / relative).is_dir())
                self.assertEqual(list((root / relative).iterdir()), [])
            payload = Path(temporary) / "portable.tar"
            image.make_payload(root, payload)
            with tarfile.open(payload) as archive:
                for relative in mountpoints:
                    member = archive.getmember("./" + relative)
                    self.assertTrue(member.isdir())
                    self.assertEqual((member.uid, member.gid), (0, 0))

    def test_man_is_not_a_required_runtime_package(self):
        manifest = Path(__file__).resolve().parents[2] / "src/daemon/Cargo.toml"
        metadata = packages.tomllib.loads(manifest.read_text())["package"]["metadata"]["generate-rpm"]
        self.assertNotIn("man", metadata.get("requires", {}))
        self.assertIn("man", metadata["recommends"])

    def test_all_manpages_are_packaged_and_rpm_assets_are_documentation(self):
        root = Path(__file__).resolve().parents[2]
        deb_pages, rpm_pages = set(), set()
        for crate in ("daemon", "pam"):
            metadata = packages.tomllib.loads((root / f"src/{crate}/Cargo.toml").read_text())["package"]["metadata"]
            for source, destination, _ in metadata["deb"]["assets"]:
                if destination.startswith("usr/share/man/"):
                    deb_pages.add(Path(source).name)
            for asset in metadata["generate-rpm"]["assets"]:
                if asset["dest"].startswith("/usr/share/man/"):
                    self.assertTrue(asset.get("doc"), asset["source"])
                    rpm_pages.add(Path(asset["source"]).name)
        expected = {Path(name).name for name in MANPAGES}
        self.assertEqual(deb_pages, expected)
        self.assertEqual(rpm_pages, expected)

    def test_native_staging_uses_queried_library_paths(self):
        for distro, library in (("arch", "usr/lib"), ("gentoo", "usr/lib64")):
            with self.subTest(distro=distro), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                source, root = work / "source", work / "root"
                for name in ("aad-tool", "libpam_himmelblau.so", "libnss_himmelblau.so"):
                    write(source, "target/release/" + name, elf())
                for relative in ("README.md", "man/man1/aad-tool.1", "src/nss/src/nss-himmelblau.tmpfiles.conf",
                                 "src/nss/src/update-nss", "target/release/locale/de/LC_MESSAGES/himmelblau.mo"):
                    write(source, relative)
                for name in MANPAGES:
                    write(source, "man/" + name, name.encode())

                def execute(command, **kwargs):
                    if command[0] == "cargo":
                        self.assertIn("--locked", command)
                        self.assertEqual(kwargs["cwd"], source)
                    if command[0] == "python3":
                        Path(command[command.index("--conf-example-output") + 1]).write_text("[global]\n")
                        self.assertIn("--gen-man", command)
                        Path(command[command.index("--man-output") + 1]).write_text("generated configuration manual\n")
                    return subprocess.CompletedProcess(command, 0, b"libc.so.6 => /usr/lib/libc.so.6\n")

                with patch.object(image, "run", side_effect=execute), \
                        patch.object(image, "native_library_directory", return_value=Path(library)):
                    image.stage_native(source, root, "amd64")
                self.assertTrue((root / library / "security/pam_himmelblau.so").is_file())
                self.assertTrue((root / library / "libnss_himmelblau.so.2").is_file())
                self.assertFalse((root / "usr/lib/systemd/system").exists())
                self.assertFalse((root / "usr/lib/systemd/user").exists())
                for name in MANPAGES:
                    expected = b"generated configuration manual\n" if name.startswith("man5/") else name.encode()
                    self.assertEqual((root / "usr/share/man" / name).read_bytes(), expected)
                self.assertEqual((source / "man/man5/himmelblau.conf.5").read_bytes(), b"man5/himmelblau.conf.5")


@unittest.skipUnless(
    all(shutil.which(tool) for tool in ("rpmbuild", "rpm", "rpm2cpio", "cpio")),
    "Native RPM tools are required for the isolated documentation-policy test",
)
class NativeDocumentationTests(unittest.TestCase):
    def test_rpm_documentation_remains_in_package_but_not_in_nodocs_install(self):
        with tempfile.TemporaryDirectory(prefix="ddi-doc-test-") as temporary:
            work = Path(temporary)
            payload = work / "payload"
            write(payload, "usr/bin/fixture", b"runtime fixture\n")
            for name in MANPAGES:
                write(payload, "usr/share/man/" + name, name.encode())
            specfile = write(work, "fixture.spec", (
                "Name: himmelblau-ddi-doc-test\nVersion: 1\nRelease: 1\n"
                "Summary: Disposable documentation test\nLicense: MIT\n"
                "BuildArch: noarch\nPrefix: /usr\nAutoReqProv: no\n"
                "%description\nDisposable documentation test.\n"
                "%install\n"
                'mkdir -p "%{buildroot}/usr"\n'
                f'cp -a "{payload}/usr/." "%{{buildroot}}/usr/"\n'
                "%files\n/usr/bin/fixture\n"
                + "".join(f"%doc /usr/share/man/{name}\n" for name in MANPAGES)
            ).encode())
            top = work / "rpmbuild"
            result = subprocess.run([
                "rpmbuild", "--define", f"_topdir {top}",
                "--define", "__os_install_post %{nil}", "--define", "__debug_install_post %{nil}",
                "-bb", str(specfile),
            ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            self.assertEqual(result.returncode, 0, result.stdout)
            built = list((top / "RPMS").rglob("*.rpm"))
            self.assertEqual(len(built), 1)
            package = built[0]
            extracted = work / "extracted"
            image.extract_package(package, extracted)
            for name in MANPAGES:
                self.assertEqual((extracted / "usr/share/man" / name).read_bytes(), name.encode())
            installed = work / "installed-usr"
            database = work / "rpmdb"
            database.mkdir()
            result = subprocess.run([
                "rpm", "--dbpath", str(database), "--prefix", str(installed),
                "--install", "--nodeps", "--noscripts", "--notriggers", "--noplugins",
                "--excludedocs", str(package),
            ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual((installed / "bin/fixture").read_bytes(), b"runtime fixture\n")
            for name in MANPAGES:
                self.assertFalse((installed / "share/man" / name).exists(), name)


class BuildTests(unittest.TestCase):
    def test_package_build_reuses_validated_actions_packages_and_records_os_identity(self):
        target = spec(kinds=["sysext"])
        target["package"] = {"source_sha": target["source_sha"], "tag": "4.1.0"}
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source, artifacts = work / "source", work / "artifacts"
            write(source, "Cargo.toml", b'[workspace.package]\nversion="4.1.0"\n')

            def package_build(source, directory, package_spec, **kwargs):
                directory.mkdir()
                (directory / "manifest.json").write_text(json.dumps({"os_release": "ID=fedora\nVERSION_ID=44\n"}))

            def container(command, **kwargs):
                self.assertEqual(command[0], "docker")
                self.assertNotIn("--privileged", command)
                self.assertFalse(any("/signing" in str(arg) for arg in command))
                if command[1] == "build":
                    self.assertEqual(list(Path(command[-1]).iterdir()), [])
                else:
                    for record in ddi.expected_images(target):
                        artifact(artifacts, record, signed=False)
                return subprocess.CompletedProcess(command, 0)

            with patch.object(packages, "run", return_value=target["source_sha"]), \
                    patch.object(packages, "build", side_effect=package_build) as build, \
                    patch.object(packages, "validate_artifacts", return_value=[
                        {"name": name, "filename": name + ".rpm"} for name in
                        ("himmelblau", "nss-himmelblau", "pam-himmelblau")
                    ]), patch.object(image, "run", side_effect=container):
                ddi.build(source, artifacts, target, container_cache_ref="ghcr.io/test/cache")
            self.assertEqual(build.call_count, 1)
            self.assertEqual(build.call_args.kwargs["container_cache_ref"], "ghcr.io/test/cache")
            self.assertTrue(all(path.name.endswith(".tar") for path in artifacts.iterdir()))

    def test_artifacts_reject_sidecars_symlinks_and_missing_images(self):
        target = spec()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for record in ddi.expected_images(target):
                artifact(directory, record, signed=False)
            ddi.validate_files(directory, target, signed=False)
            write(directory, "key.pem")
            with self.assertRaises(ValueError):
                ddi.validate_files(directory, target, signed=False)
            (directory / "key.pem").unlink()
            path = next(directory.iterdir())
            path.unlink()
            path.symlink_to("/dev/null")
            with self.assertRaises(ValueError):
                ddi.validate_files(directory, target, signed=False)

    def test_workflow_never_exposes_signing_key_to_source_builds(self):
        workflows = Path(__file__).resolve().parents[1] / "workflows"
        workflow = (workflows / "ddi-target.yml").read_text()
        build_job = workflow.split("\n  build:", 1)[1].split("\n  sign:", 1)[0]
        sign_job = workflow.split("\n  sign:", 1)[1].split("\n  publish:", 1)[0]
        publish_job = workflow.split("\n  publish:", 1)[1]
        self.assertNotIn("secrets.PACKAGE_SIGNING", build_job)
        self.assertNotIn("secrets.CLOUDSMITH", build_job)
        self.assertIn("secrets.PACKAGE_SIGNING_KEY", sign_job)
        self.assertIn("systemd erofs-utils cryptsetup-bin", sign_job)
        self.assertIn("sudo --preserve-env=TARGET_SPEC,PACKAGE_SIGNING_KEY", sign_job)
        self.assertNotIn("source_sha", sign_job)
        self.assertNotIn("secrets.CLOUDSMITH", sign_job)
        self.assertNotIn("secrets.PACKAGE_SIGNING", publish_job)
        self.assertIn("path: signed/*.raw", sign_job)
        self.assertIn("queue: max", publish_job)
        self.assertIn("uses: ./.github/workflows/ddis.yml", (workflows / "stable-packages.yml").read_text())
        scheduled = (workflows / "ddis.yml").read_text()
        self.assertIn("schedule:", scheduled)
        self.assertIn("workflow_dispatch:", scheduled)
        self.assertIn("persist-credentials: false", scheduled)

    def test_fork_smoke_test_disables_publication_and_production_cache(self):
        workflows = Path(__file__).resolve().parents[1] / "workflows"
        caller = (workflows / "ddis.yml").read_text()
        target = (workflows / "ddi-target.yml").read_text()
        self.assertIn(
            "github.repository == 'bluca/himmelblau' &&\n"
            "      github.ref == 'refs/heads/ddi' &&\n"
            "      (github.event_name == 'push' || github.event_name == 'workflow_dispatch')",
            caller,
        )
        self.assertIn("  push:\n    branches: [ddi]", caller)
        self.assertIn("ref: ${{ github.sha }}", caller)
        self.assertNotIn("github.event.repository.default_branch", caller)
        self.assertIn("REQUESTED_DISTRO: ${{ inputs.distro || 'fedora44' }}", caller)
        self.assertIn("REQUESTED_ARCHITECTURE: ${{ inputs.architecture || 'amd64' }}", caller)
        for workflow in (caller, target):
            self.assertIn("CLOUDSMITH_PACKAGE_PUBLISHER:\n        required: false", workflow)
        preflight = target.split("\n  preflight:", 1)[1].split("\n  build:", 1)[0]
        self.assertIn('run: echo "build_required=true" >> "$GITHUB_OUTPUT"', preflight)
        self.assertNotIn("CLOUDSMITH", preflight)
        self.assertNotIn("ddi.py preflight", preflight)
        self.assertIn("BUILD_CONTAINER_CACHE_REF: ''", target)
        self.assertNotIn("ghcr.io/himmelblau-idm/", target)
        self.assertIn("  publish:\n    if: ${{ false }}", target)
        self.assertIn("path: signed/*.raw", target)


class SigningTests(unittest.TestCase):
    def test_builtin_presets_replace_repository_partition_definitions(self):
        signing = image.SigningMaterial(Path("/private/key"), Path("/private/cert"), "fingerprint")
        for kind in ("portable", "sysext"):
            with self.subTest(kind=kind), patch.object(image, "run") as run:
                image.sign_image(Path("/payload"), Path("/result.raw"), "arm64", signing, kind)
            command = run.call_args.args[0]
            self.assertIn(f"--make-ddi={kind}", command)
            self.assertIn("--copy-source=/payload", command)
            self.assertIn("--architecture=arm64", command)
            self.assertIn("--offline=yes", command)
            self.assertFalse(any(arg.startswith("--definitions=") for arg in command if isinstance(arg, str)))
            self.assertEqual(run.call_args.kwargs["extra_env"],
                             {"SYSTEMD_REPART_MKFS_OPTIONS_EROFS": "--all-root -zlz4hc"})
        self.assertFalse(list((image.CONFIG / "repart.d").glob("*.conf")))
        with self.assertRaisesRegex(ValueError, "Unsupported DDI kind"):
            image.sign_image(Path("/payload"), Path("/result.raw"), "amd64", signing, "confext")

    def test_payloads_are_extracted_before_key_import_and_cleaned_up(self):
        target = spec()
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            artifacts, output = work / "artifacts", work / "signed"
            for record in ddi.expected_images(target):
                artifact(artifacts, record, signed=False)
            events, roots = [], []

            def extract(filesystem, root):
                events.append(("extract", root.name))
                root.mkdir()
                roots.append(root)

            @contextmanager
            def material(_):
                events.append(("import-key", None))
                yield image.SigningMaterial(work / "key", work / "cert", "fingerprint")
                events.append(("remove-key", None))

            def sign(root, path, architecture, signing, kind):
                events.append(("sign", kind))
                self.assertEqual(root.name, kind)
                self.assertTrue(root.is_dir())
                record = next(record for record in ddi.expected_images(target) if record["kind"] == kind)
                artifact(path.parent, record, signed=True)

            with patch.object(image, "extract_payload", side_effect=extract), \
                    patch.object(image, "signing_material", side_effect=material), \
                    patch.object(image, "sign_image", side_effect=sign):
                ddi.sign(artifacts, output, target)
            self.assertEqual(events, [
                ("extract", "sysext"), ("extract", "portable"), ("import-key", None),
                ("sign", "sysext"), ("sign", "portable"), ("remove-key", None),
            ])
            self.assertTrue(all(not root.exists() for root in roots))
            self.assertEqual({path.suffix for path in output.iterdir()}, {".raw"})

    def test_invalid_archive_fails_before_key_import(self):
        target = spec(kinds=["portable"])
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            artifact(work / "artifacts", ddi.expected_images(target)[0], signed=False)
            with patch.object(image, "extract_payload", side_effect=ValueError("bad archive")), \
                    patch.object(image, "signing_material") as signing, \
                    self.assertRaisesRegex(ValueError, "bad archive"):
                ddi.sign(work / "artifacts", work / "signed", target)
            signing.assert_not_called()
            self.assertEqual(list((work / "signed").iterdir()), [])

    def test_repart_environment_does_not_expose_the_openpgp_secret(self):
        signing = image.SigningMaterial(Path("/private/key"), Path("/private/cert"), "fingerprint")
        with patch.dict(os.environ, {"PACKAGE_SIGNING_KEY": "secret", "PACKAGE_SIGNING_PASSPHRASE": "secret"}), \
                patch.object(image.subprocess, "run") as run:
            image.sign_image(Path("/payload"), Path("/result.raw"), "amd64", signing, "sysext")
        env = run.call_args.kwargs["env"]
        self.assertNotIn("PACKAGE_SIGNING_KEY", env)
        self.assertNotIn("PACKAGE_SIGNING_PASSPHRASE", env)
        self.assertEqual(env["SYSTEMD_REPART_MKFS_OPTIONS_EROFS"], "--all-root -zlz4hc")

    def test_payload_paths_cannot_escape_the_extraction_directory(self):
        link = tarfile.TarInfo("usr/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc"
        hardlink = tarfile.TarInfo("usr/hardlink")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "/etc/shadow"
        device = tarfile.TarInfo("dev/device")
        device.type = tarfile.CHRTYPE
        for members in (
            [tarfile.TarInfo("../escape")],
            [tarfile.TarInfo("/absolute")],
            [link, tarfile.TarInfo("usr/link/shadow")],
            [hardlink], [device],
            [tarfile.TarInfo("usr/duplicate"), tarfile.TarInfo("usr/duplicate")],
        ):
            with self.subTest(paths=[member.name for member in members]), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary)
                payload = work / "payload.tar"
                with tarfile.open(payload, "w") as archive:
                    for member in members:
                        archive.addfile(member)
                with patch.object(image, "run") as run, self.assertRaises(ValueError):
                    image.extract_payload(payload, work / "root")
                run.assert_not_called()

    def test_archives_preserve_permissions_links_and_extended_attributes(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source = work / "source"
            executable = write(source, "usr/bin/tool", b"binary payload\n")
            executable.chmod(0o755)
            os.setxattr(executable, "user.himmelblau-test", b"preserved")
            (source / "bin").symlink_to("/usr/bin")
            os.link(executable, source / "usr/bin/hardlink")
            payload, root = work / "payload.tar", work / "root"
            image.make_payload(source, payload)
            image.extract_payload(payload, root)
            self.assertEqual((root / "usr/bin/tool").stat().st_mode & 0o777, 0o755)
            self.assertEqual(os.readlink(root / "bin"), "/usr/bin")
            self.assertEqual((root / "usr/bin/tool").stat().st_ino, (root / "usr/bin/hardlink").stat().st_ino)
            self.assertEqual(os.getxattr(root / "usr/bin/tool", "user.himmelblau-test"), b"preserved")


@unittest.skipUnless(
    all(shutil.which(tool) for tool in ("gpg", "gpgsm", "gpg-connect-agent", "openssl",
                                        "systemd-repart", "mkfs.erofs", "fsck.erofs", "veritysetup", "tar")),
    "Native image and signing tools are required (no containers)",
)
class NativeImageTests(unittest.TestCase):
    def test_isolated_signing_builds_self_contained_ddis_for_both_architectures(self):
        with tempfile.TemporaryDirectory(prefix="ddi-test-") as temporary:
            work = Path(temporary)
            home = work / "original"
            home.mkdir(mode=0o700)
            password = write(work, "password", b"test-key-only\n")
            password.chmod(0o600)
            gpg = ["gpg", "--homedir", str(home), "--batch", "--pinentry-mode=loopback",
                   "--passphrase-file", str(password)]
            subprocess.run(gpg + ["--quick-generate-key", "DDI test <ddi@example.invalid>",
                                  "rsa2048", "sign", "1d"], check=True, stdout=subprocess.DEVNULL)
            agent = subprocess.check_output(["gpg-connect-agent", "--homedir", str(home), "GETINFO pid", "/bye"], text=True)
            pid = int(next(line[2:] for line in agent.splitlines() if line.startswith("D ")))
            try:
                exported = subprocess.check_output(gpg + ["--armor", "--export-secret-keys"], text=True)
                env = {"PACKAGE_SIGNING_KEY": exported, "PACKAGE_SIGNING_PASSPHRASE": "test-key-only"}
                trusted = work / "trusted.crt"
                for index, architecture in enumerate(("amd64", "arm64")):
                    if index:
                        time.sleep(1.1)
                    with image.signing_material(work, env) as signing:
                        private = signing.private_key
                        self.assertEqual(private.stat().st_mode & 0o777, 0o600)
                        if index == 0:
                            shutil.copyfile(signing.certificate, trusted)
                        else:
                            self.assertNotEqual(trusted.read_bytes(), signing.certificate.read_bytes())
                        for record in ddi.expected_images(spec(architecture)):
                            root = work / (record["filename"] + ".root")
                            write(root, "usr/payload", b"actual filesystem contents\n")
                            executable = write(root, "usr/bin/fixture", b"executable payload\n")
                            executable.chmod(0o755)
                            (root / "usr/link").symlink_to("bin/fixture")
                            if record["kind"] == "portable":
                                write(root, "usr/lib/os-release", b"ID=fedora\nVERSION_ID=44\n")
                            else:
                                image.extension_release(root, {"ID": "debian", "VERSION_ID": "13"},
                                                        record["filename"], "4.1.0", architecture)
                            payload = work / (record["filename"] + ".tar")
                            image.make_payload(root, payload)
                            source = work / (record["filename"] + ".source")
                            image.extract_payload(payload, source)
                            output = work / record["filename"]
                            image.sign_image(source, output, architecture, signing, record["kind"])
                            self.verify(work, output, trusted, architecture, record["kind"])
                    self.assertFalse(private.exists())
                with self.assertRaisesRegex(RuntimeError, "intentional failure"):
                    with image.signing_material(work, env) as signing:
                        private = signing.private_key
                        raise RuntimeError("intentional failure")
                self.assertFalse(private.exists())
            finally:
                os.kill(pid, signal.SIGTERM)

    def verify(self, work, output, certificate, architecture, kind):
        with output.open("rb") as stream:
            stream.seek(512)
            header = stream.read(92)
            self.assertEqual(header[:8], b"EFI PART")
            lba, count, size = struct.unpack_from("<QII", header, 72)
            stream.seek(lba * 512)
            entries = [stream.read(size) for _ in range(count)]
            partitions = []
            for entry in entries:
                if entry[:16] == bytes(16):
                    continue
                first, last = struct.unpack_from("<QQ", entry, 32)
                stream.seek(first * 512)
                partitions.append((str(uuid.UUID(bytes_le=entry[:16])), stream.read((last - first + 1) * 512)))
        self.assertEqual([guid for guid, _ in partitions], {
            "amd64": ["4f68bce3-e8cd-4db1-96e7-fbcaf984b709", "2c7357ed-ebd2-46d9-aec1-23d437ec2bf5",
                      "41092b05-9fc8-4523-994f-2def0408b176"],
            "arm64": ["b921b045-1df0-41c3-af44-4c6f280d3fae", "df3300ce-d69f-4c92-978c-9bfb0f38d820",
                      "6db69de6-29f4-4758-a7a5-962190f00ce3"],
        }[architecture])
        data, hashes = write(work, "data.erofs", partitions[0][1]), write(work, "hashes", partitions[1][1])
        signed = json.loads(partitions[2][1].rstrip(b"\0"))
        signature = write(work, "signature.der", base64.b64decode(signed["signature"]))
        root_hash = write(work, "root-hash", signed["rootHash"].encode())
        subprocess.run(["openssl", "cms", "-verify", "-binary", "-inform", "DER", "-in", str(signature),
                        "-content", str(root_hash), "-certfile", str(certificate), "-nointern", "-noverify",
                        "-out", os.devnull], check=True)
        check = ["veritysetup", "verify", str(data), str(hashes), signed["rootHash"]]
        subprocess.run(check, check=True)
        extracted = work / (output.name + ".extracted")
        subprocess.run(["fsck.erofs", f"--extract={extracted}", str(data)], check=True)
        self.assertEqual((extracted / "usr/payload").read_text(), "actual filesystem contents\n")
        self.assertEqual((extracted / "usr/bin/fixture").stat().st_mode & 0o777, 0o755)
        self.assertEqual(os.readlink(extracted / "usr/link"), "bin/fixture")
        self.assertEqual((extracted / "usr/lib/os-release").exists(), kind == "portable")
        if kind == "sysext":
            directory = extracted / "usr/lib/extension-release.d"
            name = "extension-release." + output.name.partition("_")[0]
            self.assertEqual([path.name for path in directory.iterdir()], [name])
            metadata = image.release_fields((directory / name).read_text())
            self.assertEqual(metadata["SYSEXT_VERSION_ID"], "4.1.0")
            self.assertEqual(metadata["ARCHITECTURE"], image.ARCHITECTURES[architecture][0])
        with data.open("r+b") as stream:
            stream.seek(128)
            byte = stream.read(1)[0]
            stream.seek(128)
            stream.write(bytes([byte ^ 1]))
        self.assertNotEqual(subprocess.run(check).returncode, 0)


if __name__ == "__main__":
    unittest.main()
