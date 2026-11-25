/**
 * SPDX-FileCopyrightText: (c) 2026 Liferay, Inc. https://liferay.com
 * SPDX-License-Identifier: LGPL-2.1-or-later OR LicenseRef-Liferay-DXP-EULA-2.0.0-2023-06
 */

package com.liferay.ai.hub.service;

import com.liferay.client.extension.util.spring.boot3.client.LiferayOAuth2AccessTokenManager;
import com.liferay.client.extension.util.spring.boot3.service.BaseService;
import com.liferay.petra.string.StringBundler;

import io.fabric8.kubernetes.api.model.ObjectMeta;
import io.fabric8.kubernetes.api.model.batch.v1.Job;
import io.fabric8.kubernetes.api.model.batch.v1.JobCondition;
import io.fabric8.kubernetes.api.model.batch.v1.JobStatus;

import java.net.URI;
import java.net.URLEncoder;

import java.nio.charset.StandardCharsets;

import java.time.Instant;
import java.time.temporal.ChronoUnit;

import java.util.List;
import java.util.Objects;

import org.apache.commons.logging.Log;
import org.apache.commons.logging.LogFactory;

import org.json.JSONArray;
import org.json.JSONObject;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

/**
 * @author José Abelenda
 */
@Service
public class CrawlerJobStatusService extends BaseService {

	public CrawlerJobStatusService(KubernetesJobService kubernetesJobService) {
		_kubernetesJobService = kubernetesJobService;
	}

	private JSONArray _getActiveCrawlerJobsJSONArray() {
		String response = get(
			_liferayOAuth2AccessTokenManager.getAuthorization(
				"liferay-aihub-etc-spring-boot-oahs"),
			URI.create(
				StringBundler.concat(
					"/o/ai-hub/crawler-jobs?filter=",
					URLEncoder.encode(
						"crawlerJobStatus in ('dispatched','queued','running')",
						StandardCharsets.UTF_8),
					"&pageSize=100")));

		JSONObject jsonObject = new JSONObject(response);

		return jsonObject.optJSONArray("items");
	}

	private String _getCurrentDateTime() {
		return Instant.now(
		).truncatedTo(
			ChronoUnit.SECONDS
		).toString();
	}

	private JobCondition _getJobCondition(JobStatus jobStatus, String type) {
		List<JobCondition> jobConditions = jobStatus.getConditions();

		if (jobConditions == null) {
			return null;
		}

		for (JobCondition jobCondition : jobConditions) {
			if (type.equals(jobCondition.getType()) &&
				Objects.equals(jobCondition.getStatus(), "True")) {

				return jobCondition;
			}
		}

		return null;
	}

	private void _putIndexedDocumentStats(JSONObject jsonObject, String name) {
		try {
			String jobLog = _kubernetesJobService.getJobLog(name, 50);

			if (jobLog == null) {
				return;
			}

			for (String line : jobLog.split("\n")) {
				if (!line.contains("crawler_final_report")) {
					continue;
				}

				JSONObject lineJSONObject = new JSONObject(line);

				JSONObject jsonPayloadJSONObject = lineJSONObject.optJSONObject(
					"jsonPayload");

				if (jsonPayloadJSONObject == null) {
					jsonPayloadJSONObject = lineJSONObject;
				}

				JSONObject crawlerJSONObject =
					jsonPayloadJSONObject.optJSONObject("crawler");

				if (crawlerJSONObject == null) {
					return;
				}

				jsonObject.put(
					"indexedDocumentBytes",
					crawlerJSONObject.getLong("docs_upserted_bytes")
				).put(
					"indexedDocumentCount",
					crawlerJSONObject.getLong("docs_upserted")
				);

				return;
			}
		}
		catch (Exception exception) {
			if (_log.isWarnEnabled()) {
				_log.warn(
					"Unable to read indexed document stats for job " + name,
					exception);
			}
		}
	}

	private static final Log _log = LogFactory.getLog(
		CrawlerJobStatusService.class);

	private final KubernetesJobService _kubernetesJobService;

	@Autowired
	private LiferayOAuth2AccessTokenManager _liferayOAuth2AccessTokenManager;

}