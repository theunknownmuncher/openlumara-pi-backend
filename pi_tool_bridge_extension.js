// pi tool bridge extension for the OpenLumara pi module
// Copyright (C) 2026 theunknownmuncher <theunknownmuncher@protonmail.com>
//
// SPDX-License-Identifier: GPL-3.0-or-later
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.
//
// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
// GNU General Public License for more details.
//
// You should have received a copy of the GNU General Public License
// along with this program.  If not, see <https://www.gnu.org/licenses/>.
// Registers one pi tool per OpenLumara tool, so the pi agent can call
// OpenLumara's tools. Every OpenLumara tool call is forwarded over the
// loopback tool bridge (module: tool_bridge.py) and executed by OpenLumara
// itself; pi just carries the call and its result through its session, which
// is what makes bridged calls stream as native OpenLumara tool cards.
//
// Everything comes from the child environment the module sets:
//   PI_OPENLUMARA_BRIDGE_URL    base url of the bridge (http://127.0.0.1:port)
//   PI_OPENLUMARA_BRIDGE_TOKEN  per-spawn bearer token (never in a file)
//   PI_OPENLUMARA_TOOLS_CONFIG  optional manifest with the spawn-time tool
//                               list, registered synchronously at load so the
//                               very first turn already sees the tools
// When the url or token is unset this extension does nothing, so pi keeps
// only its own built-in tools.

import { readFileSync } from "node:fs";

export default function (pi) {
	const url = process.env.PI_OPENLUMARA_BRIDGE_URL;
	const token = process.env.PI_OPENLUMARA_BRIDGE_TOKEN;
	if (!url || !token) return;

	const auth = { authorization: "Bearer " + token };
	const registered = new Set();
	const skipped = new Map();

	function makeTool(entry) {
		return {
			name: entry.name,
			label: entry.name,
			description: entry.description || `OpenLumara tool ${entry.name}`,
			// OpenLumara serves plain JSON Schema, which is what pi wants
			parameters:
				entry.parameters && typeof entry.parameters === "object" && !Array.isArray(entry.parameters)
					? entry.parameters
					: { type: "object", properties: {} },
			async execute(_toolCallId, params) {
				let data;
				try {
					const response = await fetch(url + "/execute", {
						method: "POST",
						headers: { ...auth, "content-type": "application/json" },
						body: JSON.stringify({ tool: entry.name, arguments: params || {} }),
					});
					data = await response.json().catch(() => null);
					if (!data || typeof data.content !== "string") {
						if (!response.ok) {
							throw new Error(`OpenLumara tool bridge returned HTTP ${response.status}`);
						}
						data = { status: "success", content: "" };
					}
				} catch (error) {
					// throwing marks the pi tool result as failed: the model
					// sees the error and OpenLumara tints the card red
					throw new Error(`OpenLumara tool bridge call failed: ${error && error.message ? error.message : error}`);
				}
				return {
					content: [{ type: "text", text: data.content }],
					details: undefined,
					...(data.status === "error" ? { isError: true } : {}),
				};
			},
		};
	}

	function register(entry) {
		if (!entry || typeof entry.name !== "string" || !entry.name) return;
		if (registered.has(entry.name)) return;
		try {
			pi.registerTool(makeTool(entry));
			registered.add(entry.name);
			skipped.delete(entry.name);
		} catch (error) {
			// pi rejects some names (a tool that shadows one of its built-ins):
			// skip it, but report it - OpenLumara logs and shows which of its
			// tools the agent therefore cannot use
			skipped.set(entry.name, {
				name: entry.name,
				reason: error && error.message ? String(error.message).slice(0, 200) : "rejected by pi",
			});
		}
	}

	// fire-and-forget: tell OpenLumara what pi took and what it refused
	function report() {
		fetch(url + "/registered", {
			method: "POST",
			headers: { ...auth, "content-type": "application/json" },
			body: JSON.stringify({
				registered: [...registered],
				skipped: [...skipped.values()],
			}),
		}).catch(() => {});
	}

	// synchronous first pass from the spawn-time manifest
	const manifestPath = process.env.PI_OPENLUMARA_TOOLS_CONFIG;
	if (manifestPath) {
		try {
			const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
			for (const entry of manifest.tools || []) register(entry);
		} catch {
			// no manifest: session_start will fetch the live list instead
		}
		report();
	}

	// refresh from the bridge at every session start so tools that were
	// added or changed since pi spawned show up too
	pi.on("session_start", async () => {
		try {
			const response = await fetch(url + "/tools", { headers: auth });
			if (!response.ok) return;
			const data = await response.json();
			for (const entry of data.tools || []) register(entry);
		} catch {
			// bridge unreachable: the manifest tools (if any) stay in place
		}
		report();
	});
}
