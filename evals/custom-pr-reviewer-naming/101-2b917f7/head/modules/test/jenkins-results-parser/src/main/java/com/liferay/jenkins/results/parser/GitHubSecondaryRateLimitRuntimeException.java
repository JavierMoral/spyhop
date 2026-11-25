/**
 * SPDX-FileCopyrightText: (c) 2023 Liferay, Inc. https://liferay.com
 * SPDX-License-Identifier: LGPL-2.1-or-later OR LicenseRef-Liferay-DXP-EULA-2.0.0-2023-06
 */

package com.liferay.jenkins.results.parser;

/**
 * @author Peter Yoo
 */
public class GitHubSecondaryRateLimitRuntimeException extends RuntimeException {

	public GitHubSecondaryRateLimitRuntimeException(
		String gitHubRestApiUrl, int retryAfterSeconds, Exception exception) {

		this(gitHubRestApiUrl, retryAfterSeconds, null, exception);
	}

	public GitHubSecondaryRateLimitRuntimeException(
		String gitHubRestApiUrl, int retryAfterSeconds, String message,
		Exception exception) {

		super(message, exception);

		_gitHubRestApiUrl = gitHubRestApiUrl;
		_retryAfterSeconds = retryAfterSeconds;
	}

	public String getGitHubRestApiUrl() {
		return _gitHubRestApiUrl;
	}

	public int getRetryAfterSeconds() {
		return _retryAfterSeconds;
	}

	private final String _gitHubRestApiUrl;
	private final int _retryAfterSeconds;

}