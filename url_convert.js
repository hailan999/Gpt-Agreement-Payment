(async function () {
    console.log("⏳ 正在获取凭证并请求生成链接...");

    try {
        // 1. 动态获取当前的 Access Token
        const session = await fetch("/api/auth/session").then((r) => r.json());
        if (!session.accessToken) {
            throw new Error("无法获取 Token，请确保你已登录 ChatGPT");
        }

        // 2. 构造 Payload (关键是将模式改为 hosted)
        const payload = {
            plan_name: "chatgptplusplan",
            billing_details: {
                country: "ID",
                currency: "IDR",
            },
            entry_point:"all_plans_pricing_modal",
            promo_campaign: {
                promo_campaign_id: "plus-1-month-free",
                is_coupon_from_query_param: false,
            },
            checkout_ui_mode: "hosted",
        };

        // 3. 发送请求
        const response = await fetch(
            "https://chatgpt.com/backend-api/payments/checkout",
            {
                method: "POST",
                headers: {
                    Authorization: `Bearer ${session.accessToken}`,
                    "Content-Type": "application/json",
                },
                body: JSON.stringify(payload),
            }
        );

        const data = await response.json();

        // 4. 输出结果
        if (data.url) {
            console.clear();
            console.log(
                "%c✅ 成功生成支付长链接：",
                "color: #10a37f; font-size: 20px; font-weight: bold; margin-bottom: 10px;"
            );
            console.log(data.url);
            console.log(
                "\n%c(你可以直接点击上面的链接，或者复制发给别人)",
                "color: gray;"
            );
        } else {
            console.error("❌ 生成失败，服务器响应如下：", data);
            if (data.detail) console.error("错误详情:", data.detail);
        }
    } catch (e) {
        console.error("❌ 执行出错:", e);
    }
})();