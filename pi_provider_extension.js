// pi provider extension for the OpenLumara pi module
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
// Registers an "openlumara" provider in pi that points at the OpenAI-compatible
// endpoint configured in OpenLumara's API settings (or the pi module's manual
// override settings).
// Loaded with `pi --extension <this file>`. Everything is read from a config
// JSON whose path arrives via PI_OPENLUMARA_CONFIG; the API key itself is never
// written to that file, it is only referenced by env var name and resolved by
// pi from the child process environment (PI_OPENLUMARA_API_KEY).
// When PI_OPENLUMARA_CONFIG is unset or unreadable this extension does nothing,
// so pi stays on its own providers and auth.

import { readFileSync } from "node:fs";

export default function (pi) {
	const configPath = process.env.PI_OPENLUMARA_CONFIG;
	if (!configPath) return;

	let config;
	try {
		config = JSON.parse(readFileSync(configPath, "utf8"));
	} catch {
		return;
	}
	if (!config || !config.baseUrl) return;

	// the module writes complete model entries; the defaults here only cover
	// hand-written config files
	const models = (config.models || []).map((model) => ({
		type: "chat",
		name: model.id,
		input: ["text"],
		reasoning: false,
		cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
		contextWindow: 128000,
		maxTokens: 16384,
		...model,
	}));

	pi.registerProvider(config.provider || "openlumara", {
		name: config.displayName || "OpenLumara",
		baseUrl: config.baseUrl,
		apiKey: "$" + (config.apiKeyEnv || "PI_OPENLUMARA_API_KEY"),
		api: config.api || "openai-completions",
		models,
	});
}
