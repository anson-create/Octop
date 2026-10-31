"""Store a Jira endpoint + credential and prove it works, in one step.

The password never appears on the command line: it is read from stdin (or
prompted for on a TTY) and written straight into the secret store. Everything
else — base URL, account name, auth scheme — is configuration and is reported
back so the operator can see what was actually stored.

Two auth schemes are supported because real installs use both: an account
password (Basic) for instances that do not issue PATs, and a personal access
token (Bearer) for instances that do. Pass ``--username`` for the former.

Usage::

    printf '%s\\n' "$JIRA_PASSWORD" | python scripts/configure_jira.py \\
        --home /var/lib/octop --base-url http://jira.example.com --username anson
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path


def _read_secret() -> str:
    if sys.stdin.isatty():
        return getpass.getpass("Jira 密码 / PAT：").strip()
    return sys.stdin.readline().rstrip("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure Jira and verify the credential")
    parser.add_argument("--home", default="", help="OCTOP_HOME 数据目录（默认取环境变量或 ~/.octop）")
    parser.add_argument("--db", default="", help="SQLite 文件路径，覆盖 --home 推导值")
    parser.add_argument("--base-url", required=True, help="Jira 站点地址，如 http://jira.example.com/")
    parser.add_argument("--username", default="", help="账号名；给了就走 Basic，不给则按 PAT(Bearer) 发")
    parser.add_argument("--jql", default="", help="可选的默认 JQL")
    parser.add_argument("--workspace-name", default="默认工作区")
    parser.add_argument("--insecure", action="store_true", help="跳过 TLS 校验（自签名证书）")
    parser.add_argument("--no-probe", action="store_true", help="只存配置，不立即探测")
    args = parser.parse_args()

    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.pm.migrate import run_pm_migrations
    from octop.infra.pm.service import PMService

    if args.db:
        db_path = Path(args.db)
    elif args.home:
        db_path = Path(args.home).expanduser() / "octop.db"
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from octop.infra.utils.paths import PathLayout

        db_path = PathLayout.from_env().db
    db_path.parent.mkdir(parents=True, exist_ok=True)

    secret = _read_secret()
    if not secret:
        print("没有从 stdin 读到密码 / PAT，中止。", file=sys.stderr)
        return 2

    pool = SqlitePool(db_path)
    try:
        run_migrations(pool)
        run_pm_migrations(pool)
        service = PMService(pool)

        workspaces = service.list_workspaces()
        if workspaces:
            workspace_id = workspaces[0]["id"]
        else:
            workspace_id = service.create_workspace(name=args.workspace_name, owner_user_id=1)["id"]
        print(f"库：{db_path}")
        print(f"workspace：{workspace_id}")

        status = service.save_jira_config(
            workspace_id,
            base_url=args.base_url,
            token=secret,
            username=args.username or None,
            jql=args.jql or None,
            verify_tls=not args.insecure,
        )
        # Deliberately printed without the credential: only whether one landed.
        print(
            "已写入：base_url={base_url} auth={auth} token_set={token_set}".format(**status)
        )

        if args.no_probe:
            return 0

        print("\n=== 探测 ===")
        try:
            from octop.infra.pm.ports.base import PortError  # noqa: F401

            with service.jira_client(workspace_id) as client:
                probe = client.probe()
                print(f"身份：{probe.get('user') or '未知'}（账号 {probe.get('account') or '-'}）")
                print(f"默认 JQL 可见工单：{probe.get('open_assigned')}")
                projects = client.projects()
                print(f"可见项目 {len(projects)} 个：{', '.join(i['key'] for i in projects[:12])}")
                probe_components = client.components(projects[0]["key"]) if projects else []
                print(f"首个项目 {projects[0]['key'] if projects else '-'} 的组件 {len(probe_components)} 个")
        except Exception as exc:  # noqa: BLE001 - the operator needs the reason, whatever it is
            print(f"探测失败：{type(exc).__name__}: {str(exc)[:200]}")
            print("配置已保存，但凭据或地址有问题，修好后重跑本脚本即可。")
            return 1
    finally:
        pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
