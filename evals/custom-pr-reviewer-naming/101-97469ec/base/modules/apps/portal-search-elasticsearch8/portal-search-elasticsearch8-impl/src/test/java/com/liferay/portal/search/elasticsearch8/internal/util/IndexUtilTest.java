/**
 * SPDX-FileCopyrightText: (c) 2026 Liferay, Inc. https://liferay.com
 * SPDX-License-Identifier: LGPL-2.1-or-later OR LicenseRef-Liferay-DXP-EULA-2.0.0-2023-06
 */

package com.liferay.portal.search.elasticsearch8.internal.util;

import com.liferay.portal.kernel.json.JSONArray;
import com.liferay.portal.kernel.json.JSONObject;
import com.liferay.portal.kernel.json.JSONUtil;
import com.liferay.portal.test.rule.LiferayUnitTestRule;

import org.junit.Assert;
import org.junit.ClassRule;
import org.junit.Test;

/**
 * @author Selena Aungst
 */
public class IndexUtilTest {

	@ClassRule
	public static final LiferayUnitTestRule liferayUnitTestRule =
		LiferayUnitTestRule.INSTANCE;

	private JSONObject _createDutchOverrideSettingsJSONObject(
		JSONArray rulesJSONArray) {

		return JSONUtil.put(
			"index",
			JSONUtil.put(
				"analysis",
				JSONUtil.put(
					"filter",
					JSONUtil.put(
						"dutch_override",
						JSONUtil.put(
							"rules", rulesJSONArray
						).put(
							"type", "stemmer_override"
						)))));
	}

	private JSONObject _createDynamicTemplateJSONObject(
		String name, String type) {

		return JSONUtil.put(
			name,
			JSONUtil.put(
				"mapping", JSONUtil.put("type", type)
			).put(
				"match_mapping_type", "string"
			));
	}

	private JSONObject _createMappingsJSONObject(
		JSONArray dynamicTemplatesJSONArray) {

		return JSONUtil.put(
			"mappings",
			JSONUtil.put("dynamic_templates", dynamicTemplatesJSONArray));
	}

}