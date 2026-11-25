import os
import json
import logging
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

import aiohttp
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0") or 0)
API_BASE = "https://litecoinspace.org/api"
POLL_SECONDS = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("ltc-monitor")

CONFIG_FILE = Path("config.json")
STATE_FILE = Path("state.json")


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


config = load_json(CONFIG_FILE, {"wallets": []})
state = load_json(STATE_FILE, {"seen": {}})
config.setdefault("wallets", [])
config["wallets"] = list(dict.fromkeys(config["wallets"]))


def sats_to_ltc(value):
    return Decimal(int(value)) / Decimal(100_000_000)


class LitecoinAPI:
    def __init__(self, session):
        self.session = session

    async def get(self, endpoint):
        async with self.session.get(
            API_BASE + endpoint,
            timeout=aiohttp.ClientTimeout(total=20),
            headers={"User-Agent": "LTC-Discord-Monitor/1.0"}
        ) as response:
            response.raise_for_status()
            return await response.json()

    async def validate(self, address):
        result = await self.get(
            "/v1/validate-address/" + quote(address, safe="")
        )
        return bool(result.get("isvalid", False))

    async def address_info(self, address):
        return await self.get("/address/" + quote(address, safe=""))

    async def transactions(self, address):
        encoded = quote(address, safe="")
        confirmed = await self.get(f"/address/{encoded}/txs")
        mempool = await self.get(f"/address/{encoded}/txs/mempool")
        combined, seen = [], set()
        for tx in list(mempool) + list(confirmed):
            txid = tx.get("txid")
            if txid and txid not in seen:
                seen.add(txid)
                combined.append(tx)
        return combined

    async def mempool_transactions(self, address):
        encoded = quote(address, safe="")
        return await self.get(f"/address/{encoded}/txs/mempool")

    async def ltc_usd_price(self):
        # Public price endpoint; LTC figures remain available if this is rate-limited.
        url = "https://api.coingecko.com/api/v3/simple/price"
        async with self.session.get(
            url,
            params={"ids": "litecoin", "vs_currencies": "usd"},
            timeout=aiohttp.ClientTimeout(total=12),
            headers={"User-Agent": "LTC-Discord-Monitor/1.0"}
        ) as response:
            response.raise_for_status()
            data = await response.json()
            return Decimal(str(data["litecoin"]["usd"]))


def address_totals(info):
    chain = info.get("chain_stats", {})
    mempool = info.get("mempool_stats", {})
    received = int(chain.get("funded_txo_sum", 0)) + int(
        mempool.get("funded_txo_sum", 0)
    )
    sent = int(chain.get("spent_txo_sum", 0)) + int(
        mempool.get("spent_txo_sum", 0)
    )
    count = int(chain.get("tx_count", 0)) + int(
        mempool.get("tx_count", 0)
    )
    return received, sent, received - sent, count


def transaction_net(tx, address):
    received = sum(
        int(output.get("value", 0))
        for output in tx.get("vout", [])
        if output.get("scriptpubkey_address") == address
    )
    sent = sum(
        int((item.get("prevout") or {}).get("value", 0))
        for item in tx.get("vin", [])
        if (item.get("prevout") or {}).get("scriptpubkey_address") == address
    )
    return received - sent


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix=".", intents=intents)

session = None
api = None


def admin_only():
    async def predicate(ctx):
        return (
            ctx.guild is not None
            and ctx.author.guild_permissions.manage_guild
        )
    return commands.check(predicate)


async def alert_channel():
    if not CHANNEL_ID:
        return None
    channel = bot.get_channel(CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(CHANNEL_ID)
    return channel


@bot.command(name="bal")
async def bal(ctx, address: str = None):
    """Check any Litecoin address: .bal LTC_ADDRESS"""
    if not address:
        await ctx.reply("Usage: `.bal LTC_ADDRESS`", mention_author=False)
        return

    message = await ctx.reply("Fetching Litecoin address summary…", mention_author=False)
    try:
        if not await api.validate(address):
            await message.edit(content="That does not appear to be a valid Litecoin address.")
            return

        info = await api.address_info(address)
        chain = info.get("chain_stats", {})
        mempool = info.get("mempool_stats", {})

        confirmed_received = int(chain.get("funded_txo_sum", 0))
        confirmed_sent = int(chain.get("spent_txo_sum", 0))
        unconfirmed_received = int(mempool.get("funded_txo_sum", 0))
        unconfirmed_sent = int(mempool.get("spent_txo_sum", 0))

        confirmed_balance = confirmed_received - confirmed_sent
        unconfirmed_balance = unconfirmed_received - unconfirmed_sent
        final_balance = confirmed_balance + unconfirmed_balance
        total_received = confirmed_received + unconfirmed_received
        total_sent = confirmed_sent + unconfirmed_sent

        # Sum positive net changes from transactions currently in the mempool.
        # This is the amount presently pending to arrive, not lifetime received.
        pending_txs = await api.mempool_transactions(address)
        currently_receiving = sum(
            max(transaction_net(tx, address), 0)
            for tx in pending_txs
        )

        confirmed_count = int(chain.get("tx_count", 0))
        unconfirmed_count = int(mempool.get("tx_count", 0))
        total_count = confirmed_count + unconfirmed_count

        try:
            usd_price = await api.ltc_usd_price()
        except Exception:
            log.warning("LTC/USD price unavailable; showing LTC only.")
            usd_price = None

        def amount_text(sats):
            amount = sats_to_ltc(sats)
            if usd_price is None:
                return f"`{amount:.8f} LTC`"
            return f"`{amount:.8f} LTC` (~${amount * usd_price:,.2f})"

        address_url = f"https://litecoinspace.org/address/{quote(address, safe='')}"
        description = (
            f"🔗 **Address:** [{address}]({address_url})\n"
            f"› **Transactions:** `{total_count}` · Unconfirmed: `{unconfirmed_count}`\n\n"
            f"**Balances**\n"
            f"› Confirmed: {amount_text(confirmed_balance)}\n"
            f"› Unconfirmed: {amount_text(unconfirmed_balance)}\n"
            f"› **Currently receiving:** {amount_text(currently_receiving)}\n"
            f"› **Final:** {amount_text(final_balance)}\n\n"
            f"**Totals**\n"
            f"› Received: {amount_text(total_received)}\n"
            f"› Sent: {amount_text(total_sent)}"
        )

        embed = discord.Embed(
            title="Litecoin Address Summary",
            description=description,
            color=discord.Color.from_rgb(46, 204, 113)
        )
        embed.set_footer(text="Litecoin blockchain data • USD values are estimates")
        await message.edit(content=None, embed=embed)

    except aiohttp.ClientResponseError as exc:
        log.exception("Litecoin explorer HTTP error")
        await message.edit(content=f"Litecoin explorer error (HTTP {exc.status}). Try again shortly.")
    except Exception:
        log.exception("Address lookup failed")
        await message.edit(content="Couldn't fetch this address summary right now. Please try again later.")



# ================= USDT MULTI-NETWORK CHECKER =================
# Usage: .usdt <polygon|bep20|tron|eth|sol> <wallet_address>

USDT_NETWORKS = {
    "polygon": {
        "label": "Polygon PoS USDT",
        "rpc": "https://polygon-bor-rpc.publicnode.com",
        "contract": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
        "decimals": 6, "explorer": "https://polygonscan.com/address/",
        "kind": "evm",
        "note": "Polygon USDT token deployment; confirm this is the token your recipient accepts."
    },
    "bep20": {
        "label": "BNB Smart Chain (Binance-Peg BSC-USD)",
        "rpc": "https://bsc-dataseed.binance.org",
        "contract": "0x55d398326f99059fF775485246999027B3197955",
        "decimals": 18, "explorer": "https://bscscan.com/address/",
        "kind": "evm",
        "note": "Binance-Peg BSC-USD representation; not a Tether-issued deployment."
    },
    "eth": {
        "label": "Ethereum ERC-20 USDT",
        "rpc": "https://ethereum-rpc.publicnode.com",
        "contract": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        "decimals": 6, "explorer": "https://etherscan.io/address/",
        "kind": "evm",
        "note": "Tether USD₮ Ethereum contract."
    },
    "tron": {
        "label": "TRON TRC-20 USDT",
        "contract": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        "decimals": 6, "explorer": "https://tronscan.org/#/address/",
        "kind": "tron",
        "note": "Tether USD₮ TRON contract."
    },
    "sol": {
        "label": "Solana SPL USDT",
        "mint": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
        "rpc": "https://api.mainnet-beta.solana.com",
        "decimals": 6, "explorer": "https://solscan.io/account/",
        "kind": "solana",
        "note": "Tether USD₮ Solana mint."
    }
}


async def rpc_post(url, payload):
    async with session.post(
        url, json=payload,
        timeout=aiohttp.ClientTimeout(total=20),
        headers={"Content-Type": "application/json"}
    ) as response:
        response.raise_for_status()
        result = await response.json()
        if result.get("error"):
            raise RuntimeError(str(result["error"]))
        return result


async def evm_usdt_balance(network, address):
    cfg = USDT_NETWORKS[network]
    data = "0x70a08231" + address[2:].lower().rjust(64, "0")
    result = await rpc_post(cfg["rpc"], {
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": cfg["contract"], "data": data}, "latest"]
    })
    return Decimal(int(result["result"], 16)) / (Decimal(10) ** cfg["decimals"])


async def tron_usdt_balance(address):
    url = "https://api.trongrid.io/v1/accounts/" + quote(address, safe="")
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as response:
        response.raise_for_status()
        data = await response.json()
    accounts = data.get("data", [])
    if not accounts:
        return Decimal(0)
    contract = USDT_NETWORKS["tron"]["contract"]
    for token in accounts[0].get("trc20", []):
        if contract in token:
            return Decimal(token[contract]) / Decimal(10**6)
    return Decimal(0)


async def solana_usdt_balance(owner):
    cfg = USDT_NETWORKS["sol"]
    result = await rpc_post(cfg["rpc"], {
        "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
        "params": [
            owner, {"mint": cfg["mint"]},
            {"encoding": "jsonParsed", "commitment": "confirmed"}
        ]
    })
    total = Decimal(0)
    for item in result.get("result", {}).get("value", []):
        token_amount = item["account"]["data"]["parsed"]["info"]["tokenAmount"]
        total += Decimal(token_amount["amount"]) / (
            Decimal(10) ** int(token_amount["decimals"])
        )
    return total


@bot.command(name="usdt")
async def usdt(ctx, network: str = None, address: str = None):
    """Check USDT balance: .usdt polygon|bep20|tron|eth|sol ADDRESS"""
    if not network or not address:
        await ctx.reply(
            "Usage: `.usdt <polygon|bep20|tron|eth|sol> WALLET_ADDRESS`",
            mention_author=False
        )
        return

    network = network.lower().strip()
    if network not in USDT_NETWORKS:
        await ctx.reply(
            "Choose a network: `polygon`, `bep20`, `tron`, `eth`, or `sol`.",
            mention_author=False
        )
        return

    cfg = USDT_NETWORKS[network]
    message = await ctx.reply("Checking USDT balance…", mention_author=False)
    try:
        if cfg["kind"] == "evm":
            if not (address.startswith("0x") and len(address) == 42):
                await message.edit(content="This network needs a 42-character `0x` EVM wallet address.")
                return
            balance = await evm_usdt_balance(network, address)
        elif cfg["kind"] == "tron":
            if not address.startswith("T"):
                await message.edit(content="TRON wallet addresses normally start with `T`. Check the network.")
                return
            balance = await tron_usdt_balance(address)
        else:
            if not (32 <= len(address) <= 44):
                await message.edit(content="That does not look like a Solana wallet address.")
                return
            balance = await solana_usdt_balance(address)

        address_url = cfg["explorer"] + quote(address, safe="")
        embed = discord.Embed(
            title="USDT Address Summary",
            description=(
                f"🌐 **Network:** {cfg['label']}\\n"
                f"🔗 **Address:** [{address}]({address_url})\\n\\n"
                f"**USDT Balance**\\n"
                f"› **Confirmed:** `{balance:,.6f} USDT`\\n"
                f"› Unconfirmed incoming: `Not available in this balance lookup`\\n"
                f"› Currently receiving: `Not available in this balance lookup`"
            ),
            color=discord.Color.from_rgb(38, 166, 154)
        )
        embed.add_field(name="Token details", value=cfg["note"], inline=False)
        embed.set_footer(text="Public blockchain RPC/API • Balance only")
        await message.edit(content=None, embed=embed)

    except aiohttp.ClientResponseError as exc:
        log.exception("USDT provider HTTP error")
        await message.edit(content=f"USDT provider error (HTTP {exc.status}). Try again shortly.")
    except Exception:
        log.exception("USDT lookup failed")
        await message.edit(content="Couldn't retrieve this USDT balance. Check the address/network and try again.")


@bot.command(name="monitor")
@admin_only()
async def monitor(ctx, address: str = None):
    """Start monitoring an address for new transactions."""
    if not address:
        await ctx.reply("Usage: `.monitor LTC_ADDRESS`")
        return
    try:
        if not await api.validate(address):
            await ctx.reply("That does not appear to be a valid Litecoin address.")
            return
        if address in config["wallets"]:
            await ctx.reply("That address is already being monitored.")
            return

        # Baseline current history so old transactions do not trigger alerts.
        txs = await api.transactions(address)
        config["wallets"].append(address)
        state.setdefault("seen", {})[address] = [tx["txid"] for tx in txs if tx.get("txid")]
        save_json(CONFIG_FILE, config)
        save_json(STATE_FILE, state)
        await ctx.reply(f"Now monitoring `{address}`. New transactions will be posted in the configured alert channel.")
    except Exception:
        log.exception("Could not start monitoring")
        await ctx.reply("Couldn't start monitoring this address. Try again later.")


@bot.command(name="unmonitor")
@admin_only()
async def unmonitor(ctx, address: str = None):
    if not address:
        await ctx.reply("Usage: `.unmonitor LTC_ADDRESS`")
        return
    if address not in config["wallets"]:
        await ctx.reply("That address is not being monitored.")
        return
    config["wallets"].remove(address)
    save_json(CONFIG_FILE, config)
    await ctx.reply(f"Stopped monitoring `{address}`.")


@bot.command(name="wallets")
@admin_only()
async def wallets(ctx):
    if not config["wallets"]:
        await ctx.reply("No Litecoin addresses are currently monitored.")
        return
    lines = [f"`{address}`" for address in config["wallets"]]
    text = "\n".join(lines)
    if len(text) > 1900:
        text = text[:1900] + "\n…"
    await ctx.reply("**Monitored Litecoin addresses**\n" + text)


async def post_transaction_alert(address, tx, net, channel):
    incoming = net > 0
    txid = tx["txid"]
    confirmed = tx.get("status", {}).get("confirmed", False)

    embed = discord.Embed(
        title="LTC received" if incoming else "LTC sent",
        color=discord.Color.green() if incoming else discord.Color.red()
    )
    embed.add_field(name="Wallet", value=f"`{address}`", inline=False)
    embed.add_field(name="Amount (net)", value=f"{sats_to_ltc(abs(net)):.8f} LTC", inline=True)
    embed.add_field(name="Status", value="Confirmed" if confirmed else "Unconfirmed", inline=True)
    embed.add_field(name="Transaction", value=f"[View on Litecoin explorer](https://litecoinspace.org/tx/{txid})", inline=False)
    await channel.send(embed=embed)


async def check_monitored_wallet(address):
    txs = await api.transactions(address)
    seen_map = state.setdefault("seen", {})

    # First-time safety baseline: don't announce transactions from before monitoring.
    if address not in seen_map:
        seen_map[address] = [tx["txid"] for tx in txs if tx.get("txid")]
        save_json(STATE_FILE, state)
        return

    known = set(seen_map[address])
    new_txs = [tx for tx in reversed(txs) if tx.get("txid") not in known]

    channel = await alert_channel()
    for tx in new_txs:
        net = transaction_net(tx, address)
        if net and channel is not None:
            await post_transaction_alert(address, tx, net, channel)
        known.add(tx["txid"])

    # Save recent IDs to prevent repeat alerts. This is a bounded recent cache.
    seen_map[address] = list(known)[-500:]
    save_json(STATE_FILE, state)


@tasks.loop(seconds=POLL_SECONDS)
async def monitor_loop():
    for address in list(config["wallets"]):
        try:
            await check_monitored_wallet(address)
        except Exception:
            log.exception("Monitoring failed for %s", address)


@monitor_loop.before_loop
async def before_monitor_loop():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    if not monitor_loop.is_running():
        monitor_loop.start()


async def main():
    global session, api

    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing. Add your bot token to the .env file.")
    if config["wallets"] and not CHANNEL_ID:
        log.warning("DISCORD_CHANNEL_ID is not set; transaction alerts cannot be posted.")

    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        api = LitecoinAPI(session)
        async with bot:
            await bot.start(TOKEN)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
