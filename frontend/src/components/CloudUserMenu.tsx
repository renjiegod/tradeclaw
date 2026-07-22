import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Avatar, Button, Dropdown, type MenuProps } from "antd";
import { ApiOutlined, LogoutOutlined, UserOutlined } from "@ant-design/icons";

import { beginAuthRedirect } from "../api";

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
  const navigate = useNavigate();

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
    // 先置位"正在回登录入口":清除 cookie 后到整页导航卸载前的窗口里,页面上仍在
    // 飞行的后台请求会以失效 cookie 拿到 401,置位后这些 401 不再弹「请求失败」。
    beginAuthRedirect(false); // 本函数自己整页跳转,无需 beginAuthRedirect 再跳
    try {
      await fetch("/api/console/v1/auth/logout", {
        method: "POST",
        headers: { "X-Requested-With": "console" },
        credentials: "include",
      });
    } finally {
      window.location.href = "/"; // 网关对未登录导航 302 到 /console/ 登录页
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
      icon: <ApiOutlined />,
      label: "数据接入",
      // 数据控制台已并入本前端（/data_console 模块），走 SPA 内部跳转；
      // 旧 /console SPA 不再从该域名提供。
      onClick: () => {
        navigate("/data_console");
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
