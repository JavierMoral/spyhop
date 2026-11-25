/**
 * SPDX-FileCopyrightText: (c) 2025 Liferay, Inc. https://liferay.com
 * SPDX-License-Identifier: LGPL-2.1-or-later OR LicenseRef-Liferay-DXP-EULA-2.0.0-2023-06
 */

package com.liferay.portal.search.elasticsearch8.internal.settings;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.dataformat.yaml.YAMLFactory;

import com.liferay.petra.string.CharPool;
import com.liferay.petra.string.StringBundler;
import com.liferay.petra.string.StringPool;
import com.liferay.portal.kernel.json.JSONException;
import com.liferay.portal.kernel.json.JSONFactoryUtil;
import com.liferay.portal.kernel.json.JSONObject;
import com.liferay.portal.kernel.json.JSONUtil;
import com.liferay.portal.kernel.util.ArrayUtil;
import com.liferay.portal.search.elasticsearch8.internal.util.IndexUtil;
import com.liferay.portal.search.spi.index.configuration.contributor.helper.SettingsHelper;

import java.util.List;

import org.apache.commons.lang.StringUtils;

/**
 * @author André de Oliveira
 */
public class SettingsHelperImpl implements SettingsHelper {

	@Override
	public String get(String key) {
		String[] keyParts = key.split("\\.");

		if (keyParts.length > 1) {
			JSONObject settingJSONObject = _settingsJSONObject;

			for (int i = 0; i < (keyParts.length - 1); i++) {
				settingJSONObject = settingJSONObject.getJSONObject(
					keyParts[i]);
			}

			return settingJSONObject.getString(keyParts[keyParts.length - 1]);
		}

		return _settingsJSONObject.getString(key, null);
	}

	public JSONObject getSettingsJSONObject() {
		return _settingsJSONObject;
	}

	public void put(String key, boolean value) {
		put(key, String.valueOf(value));
	}

	private JSONObject _createJSONObject(String jsonString) {
		try {
			return JSONFactoryUtil.createJSONObject(jsonString);
		}
		catch (JSONException jsonException) {
			throw new RuntimeException(jsonException);
		}
	}

	private final JSONObject _settingsJSONObject =
		JSONFactoryUtil.createJSONObject();

}