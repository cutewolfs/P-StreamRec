import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ENTRYPOINT = ROOT / "docker" / "entrypoint.sh"
DOCKERFILE = ROOT / "Dockerfile"


class DockerEntrypointTests(unittest.TestCase):
    def run_shell(self, script, env=None):
        merged_env = os.environ.copy()
        merged_env.update(env or {})
        result = subprocess.run(
            ["sh", "-c", script],
            cwd=ROOT,
            env=merged_env,
            text=True,
            capture_output=True,
            check=True,
        )
        return result.stdout

    def test_entrypoint_has_valid_shell_syntax(self):
        subprocess.run(["sh", "-n", str(ENTRYPOINT)], check=True)

    def test_dockerfile_uses_absolute_verified_entrypoint(self):
        dockerfile = DOCKERFILE.read_text()

        self.assertIn("COPY docker/entrypoint.sh /usr/local/bin/pstreamrec-entrypoint", dockerfile)
        self.assertIn("test -x /usr/local/bin/pstreamrec-entrypoint", dockerfile)
        self.assertIn("PSTREAMREC_ENTRYPOINT_TESTING=1 /usr/local/bin/pstreamrec-entrypoint", dockerfile)
        self.assertIn('ENTRYPOINT ["/usr/local/bin/pstreamrec-entrypoint"]', dockerfile)
        self.assertNotIn('ENTRYPOINT ["pstreamrec-entrypoint"]', dockerfile)

    def test_dns_cache_resolv_conf_uses_local_cache_and_preserves_options(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            original_resolv = tmpdir_path / "original-resolv.conf"
            target_resolv = tmpdir_path / "resolv.conf"
            original_resolv.write_text(
                textwrap.dedent(
                    """\
                    nameserver 1.1.1.1
                    search lan
                    options ndots:0
                    """
                )
            )
            target_resolv.write_text(original_resolv.read_text())

            output = self.run_shell(
                ". ./docker/entrypoint.sh; write_cached_resolv_conf; cat \"$PSTREAMREC_RESOLV_CONF\"",
                env={
                    "PSTREAMREC_ENTRYPOINT_TESTING": "1",
                    "PSTREAMREC_DNS_CACHE_ORIGINAL_RESOLV": str(original_resolv),
                    "PSTREAMREC_RESOLV_CONF": str(target_resolv),
                },
            )

        self.assertEqual(
            output,
            "nameserver 127.0.0.1\nsearch lan\noptions ndots:0\n",
        )

    def test_dns_cache_upstreams_env_writes_nameserver_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_resolv = Path(tmpdir) / "original-resolv.conf"

            output = self.run_shell(
                ". ./docker/entrypoint.sh; write_upstream_resolv_file; cat \"$PSTREAMREC_DNS_CACHE_ORIGINAL_RESOLV\"",
                env={
                    "PSTREAMREC_ENTRYPOINT_TESTING": "1",
                    "PSTREAMREC_DNS_CACHE_ORIGINAL_RESOLV": str(original_resolv),
                    "PSTREAMREC_DNS_CACHE_UPSTREAMS": "1.1.1.1, 1.0.0.1",
                },
            )

        self.assertEqual(output, "nameserver 1.1.1.1\nnameserver 1.0.0.1\n")

    def test_dns_cache_detects_localhost_upstream_without_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            original_resolv = Path(tmpdir) / "original-resolv.conf"
            original_resolv.write_text("nameserver 127.0.0.1\n")

            output = self.run_shell(
                ". ./docker/entrypoint.sh; if has_unsafe_loopback_upstream; then echo unsafe; else echo safe; fi",
                env={
                    "PSTREAMREC_ENTRYPOINT_TESTING": "1",
                    "PSTREAMREC_DNS_CACHE_ORIGINAL_RESOLV": str(original_resolv),
                },
            )

        self.assertEqual(output, "unsafe\n")


if __name__ == "__main__":
    unittest.main()
