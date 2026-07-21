import { useEffect, useState } from "react";
import { Avatar, Button, Dropdown, type MenuProps } from "antd";
import { KeyOutlined, LogoutOutlined, UserOutlined } from "@ant-design/icons";

/**
 * Cloud-only header chrome (logged-in user avatar / name / logout / console
 * link). Rendered ONLY when the deployment is "cloud" — in the single-machine
 * (local) build it returns null, so the SAME frontend bundle serves both
 * without a fork. Identity comes from the same-origin dytc console API
 * (console.doyoutrade.cloud/api/console/v1/*), which the gateway fronts.
 */
type Me = {
  user: { github_login: string; avatar_url?: string | null };
  tenant: { id: string; name: string };
  quota?: { daily_requests: number; used_today: number; remaining_today: number };
};

export function CloudUserMenu({ mode }: { mode?: string | null }) {
  const [me, setMe] = useState<Me | null>(null);

  useEffect(() => {
    if (mode !== "cloud") return;
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch("/api/console/v1/me", { credentials: "include" });
        if (r.ok && !cancelled) setMe((await r.json()) as Me);
      } catch {
        // Not logged in / console unreachable: keep an anonymous menu; the
        // logout action still clears any stale cookie and re-triggers login.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [mode]);

  if (mode !== "cloud") return null;

  const logout = async () => {
    try {
      await fetch("/api/console/v1/auth/logout", {
        method: "POST",
        headers: { "X-Requested-With": "console" },
        credentials: "include",
      });
    } finally {
      window.location.href = "/"; // → 302 to GitHub login
    }
  };

  const items: MenuProps["items"] = [
    ...(me?.quota
      ? [
          {
            key: "quota",
            disabled: true,
            label: `今日额度 ${me.quota.remaining_today}/${me.quota.daily_requests}`,
          },
          { type: "divider" as const },
        ]
      : []),
    {
      key: "console",
      icon: <KeyOutlined />,
      label: "API Keys / 用量",
      onClick: () => {
        window.location.href = "/console/";
      },
    },
    { type: "divider" },
    { key: "logout", icon: <LogoutOutlined />, label: "退出登录", onClick: logout },
  ];

  const name = me?.user?.github_login ?? "账户";
  return (
    <Dropdown menu={{ items }} placement="bottomRight" trigger={["click"]}>
      <Button type="text" className="!flex !items-center gap-2 !px-2" data-testid="cloud-user-menu">
        <Avatar size="small" src={me?.user?.avatar_url ?? undefined} icon={<UserOutlined />} />
        <span className="max-w-[140px] truncate">{name}</span>
      </Button>
    </Dropdown>
  );
}
