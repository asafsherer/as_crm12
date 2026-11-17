// Cloudflare Pages Function: /api/lead
// The visitor's browser posts the contact form here. This function adds the secret token and forwards it to the CRM,
// so the token never appears in the website's code.
//
// Settings (Pages project -> Settings -> Variables and Secrets):
//   CRM_URL       e.g. https://crm.as-finances.com      (plain text)
//   INTAKE_TOKEN  the same value as LEAD_INTAKE_TOKEN in the CRM   (SECRET)
const ALLOWED_ORIGINS = ["https://as-finances.com", "https://www.as-finances.com"];
const FIELDS = ["name", "first_name", "last_name", "phone", "email", "message", "interest", "consent",
                "call_time", "owns_property", "enforcement_file", "utm_source", "utm_medium", "utm_campaign", "utm_term", "page", "website"];

const reply = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } });

export async function onRequestPost({ request, env }) {
  const origin = request.headers.get("Origin") || "";
  if (!ALLOWED_ORIGINS.includes(origin)) return reply({ ok: false }, 403);
  let data;
  try { data = await request.json(); } catch { return reply({ ok: false }, 400); }
  const body = {};
  for (const k of FIELDS) if (data[k] !== undefined) body[k] = String(data[k]).slice(0, 2000);
  const res = await fetch(env.CRM_URL.replace(/\/$/, "") + "/api/intake/", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Intake-Token": env.INTAKE_TOKEN },
    body: JSON.stringify(body),
  });
  // pass validation problems (e.g. missing phone) back to the form; hide everything else
  if (res.status === 400) return reply(await res.json().catch(() => ({ ok: false })), 400);
  return reply({ ok: res.ok }, res.ok ? 200 : 502);
}
