/**
 * SPDX-FileCopyrightText: (c) 2025 Liferay, Inc. https://liferay.com
 * SPDX-License-Identifier: LGPL-2.1-or-later OR LicenseRef-Liferay-DXP-EULA-2.0.0-2023-06
 */

package com.liferay.osb.patcher.util;

import com.google.cloud.storage.Blob;
import com.google.cloud.storage.BlobId;
import com.google.cloud.storage.Storage;
import com.google.cloud.storage.StorageOptions;

import com.liferay.osb.patcher.configuration.PatcherConfiguration;
import com.liferay.osb.patcher.constants.HelpCenterConstants;
import com.liferay.osb.patcher.model.PatcherBuild;
import com.liferay.petra.string.StringBundler;
import com.liferay.petra.string.StringPool;
import com.liferay.portal.configuration.module.configuration.ConfigurationProviderUtil;
import com.liferay.portal.kernel.exception.PortalException;
import com.liferay.portal.kernel.json.JSONArray;
import com.liferay.portal.kernel.json.JSONFactoryUtil;
import com.liferay.portal.kernel.json.JSONObject;
import com.liferay.portal.kernel.json.JSONUtil;
import com.liferay.portal.kernel.language.LanguageUtil;
import com.liferay.portal.kernel.log.Log;
import com.liferay.portal.kernel.log.LogFactoryUtil;
import com.liferay.portal.kernel.servlet.HttpHeaders;
import com.liferay.portal.kernel.util.ContentTypes;
import com.liferay.portal.kernel.util.DigesterUtil;
import com.liferay.portal.kernel.util.Http;
import com.liferay.portal.kernel.util.HttpUtil;
import com.liferay.portal.kernel.util.LocaleUtil;

import java.io.InputStream;

import java.net.HttpURLConnection;
import java.net.URL;

import java.nio.channels.Channels;

import org.apache.commons.io.IOUtils;

/**
 * @author Zsolt Balogh
 */
public class HelpCenterUtil {

	protected static String getMD5Checksum(InputStream fileInputStream) {
		return DigesterUtil.digestHex(DigesterUtil.MD5, fileInputStream);
	}

	protected static void uploadAttachment(
			InputStream fileInputStream, String fileSize, String gcsSessionURL)
		throws Exception {

		URL url = new URL(gcsSessionURL);

		HttpURLConnection httpURLConnection =
			(HttpURLConnection)url.openConnection();

		httpURLConnection.setDoInput(true);
		httpURLConnection.setDoOutput(true);
		httpURLConnection.setRequestMethod("PUT");
		httpURLConnection.setRequestProperty("User-Agent", _PATCHER_USER_AGENT);
		httpURLConnection.setRequestProperty("Content-Length", fileSize);
		httpURLConnection.setRequestProperty(
			"Content-Type", "application/octet-stream");

		IOUtils.copy(fileInputStream, httpURLConnection.getOutputStream());

		int responseCode = httpURLConnection.getResponseCode();

		InputStream responseInputStream = httpURLConnection.getInputStream();

		String responseBody = IOUtils.toString(
			responseInputStream, StringPool.UTF8);

		if (responseCode != HttpURLConnection.HTTP_OK) {
			_log.error(
				StringBundler.concat(
					"Response code ", responseCode, ": ", responseBody));

			throw new PortalException("failed-to-upload-file");
		}

		httpURLConnection.disconnect();
	}

	private static final String _PATCHER_USER_AGENT = "OSB Patcher Portal/7.4";

	private static final Log _log = LogFactoryUtil.getLog(HelpCenterUtil.class);

	private static String _accessToken;
	private static long _tokenExpirationTime;

}