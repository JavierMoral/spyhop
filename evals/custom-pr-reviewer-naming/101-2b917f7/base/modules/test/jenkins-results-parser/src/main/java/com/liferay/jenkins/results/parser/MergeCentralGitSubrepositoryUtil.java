/**
 * SPDX-FileCopyrightText: (c) 2000 Liferay, Inc. https://liferay.com
 * SPDX-License-Identifier: LGPL-2.1-or-later OR LicenseRef-Liferay-DXP-EULA-2.0.0-2023-06
 */

package com.liferay.jenkins.results.parser;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;

import java.util.ArrayList;
import java.util.List;
import java.util.Properties;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import org.apache.commons.lang.StringUtils;

import org.json.JSONArray;
import org.json.JSONObject;

/**
 * @author Michael Hashimoto
 */
public class MergeCentralGitSubrepositoryUtil {

	public static void createGitSubrepositoryMergePullRequests(
			String centralWorkingDirectory, String centralUpstreamBranchName,
			String receiverUserName, String senderUserName,
			String topLevelBranchName)
		throws IOException {

		GitWorkingDirectory centralGitWorkingDirectory =
			GitWorkingDirectoryFactory.newGitWorkingDirectory(
				centralUpstreamBranchName, centralWorkingDirectory);

		File modulesDir = new File(
			centralGitWorkingDirectory.getWorkingDirectory(), "modules");

		if (!modulesDir.exists()) {
			return;
		}

		List<String> failedGitrepoPaths = new ArrayList<>();
		List<String> subrepoMergeBlacklist =
			JenkinsResultsParserUtil.getBuildPropertyAsList(
				false, "subrepo.merge.blacklist");

		List<File> gitrepoFiles = JenkinsResultsParserUtil.findFiles(
			modulesDir, ".gitrepo");

		for (File gitrepoFile : gitrepoFiles) {
			try {
				Properties gitrepoProperties = _getPropertiesFromGitrepoFile(
					gitrepoFile);

				String remote = gitrepoProperties.getProperty("remote");

				Matcher matcher = _githubRemotePattern.matcher(remote);

				if (matcher.find() && !subrepoMergeBlacklist.isEmpty() &&
					subrepoMergeBlacklist.contains(
						matcher.group("gitSubrepositoryName"))) {

					continue;
				}

				CentralGitSubrepository centralGitSubrepository =
					new CentralGitSubrepository(
						gitrepoFile, centralUpstreamBranchName);

				if (!centralGitSubrepository.isAutoPullEnabled()) {
					continue;
				}

				String mergeBranchName = _getMergeBranchName(
					centralUpstreamBranchName,
					centralGitSubrepository.getGitSubrepositoryName(),
					centralGitSubrepository.
						getGitSubrepositoryUpstreamCommit());

				if (centralGitSubrepository.isCentralPullRequestCandidate()) {
					GitRemote upstreamGitRemote =
						centralGitWorkingDirectory.getGitRemote("upstream");

					if (!centralGitWorkingDirectory.remoteGitBranchExists(
							mergeBranchName, upstreamGitRemote)) {

						LocalGitBranch topLevelLocalGitBranch =
							centralGitWorkingDirectory.getLocalGitBranch(
								topLevelBranchName, true);

						LocalGitBranch mergeLocalGitBranch =
							_createMergeLocalGitBranch(
								centralGitWorkingDirectory, mergeBranchName,
								topLevelLocalGitBranch);

						_commitCiMergeFile(
							centralGitWorkingDirectory, centralGitSubrepository,
							gitrepoFile);

						_pushMergeLocalGitBranchToRemote(
							centralGitWorkingDirectory, mergeLocalGitBranch,
							senderUserName);
					}

					_createMergePullRequest(
						centralGitWorkingDirectory, centralGitSubrepository,
						mergeBranchName, receiverUserName, senderUserName);
				}

				_deleteStalePulls(
					centralGitWorkingDirectory, centralGitSubrepository,
					mergeBranchName, receiverUserName);

				_deleteStaleBranches(
					centralGitWorkingDirectory, centralGitSubrepository,
					mergeBranchName);
			}
			catch (Exception exception) {
				failedGitrepoPaths.add(gitrepoFile.getParent());

				exception.printStackTrace();
			}
		}

		if (!failedGitrepoPaths.isEmpty()) {
			String message = JenkinsResultsParserUtil.combine(
				"Unable to create a pull to merge these subrepositories:\n",
				StringUtils.join(failedGitrepoPaths, "\n"));

			Properties buildProperties =
				JenkinsResultsParserUtil.getBuildProperties();

			NotificationUtil.sendEmail(
				message, "jenkins", "Merge central Git subrepository",
				buildProperties.getProperty(
					"email.list[merge-central-subrepository]"));

			throw new RuntimeException(message);
		}
	}

	private static void _commitCiMergeFile(
			GitWorkingDirectory centralGitWorkingDirectory,
			CentralGitSubrepository centralGitSubrepository, File gitrepoFile)
		throws IOException {

		String gitSubrepositoryUpstreamCommit =
			centralGitSubrepository.getGitSubrepositoryUpstreamCommit();

		String ciMergeFilePath = _getCiMergeFilePath(
			centralGitWorkingDirectory, gitrepoFile);

		JenkinsResultsParserUtil.write(
			new File(
				centralGitWorkingDirectory.getWorkingDirectory(),
				ciMergeFilePath),
			gitSubrepositoryUpstreamCommit);

		centralGitWorkingDirectory.stageFileInCurrentLocalGitBranch(
			ciMergeFilePath);

		centralGitWorkingDirectory.commitStagedFilesToCurrentBranch(
			"Create " + ciMergeFilePath + ".");
	}

	private static LocalGitBranch _createMergeLocalGitBranch(
		GitWorkingDirectory centralGitWorkingDirectory, String mergeBranchName,
		LocalGitBranch topLevelLocalGitBranch) {

		centralGitWorkingDirectory.reset("--hard");

		centralGitWorkingDirectory.checkoutLocalGitBranch(
			topLevelLocalGitBranch);

		LocalGitBranch mergeLocalGitBranch =
			centralGitWorkingDirectory.getLocalGitBranch(mergeBranchName);

		if (mergeLocalGitBranch != null) {
			centralGitWorkingDirectory.deleteLocalGitBranch(
				mergeLocalGitBranch);
		}

		mergeLocalGitBranch = centralGitWorkingDirectory.createLocalGitBranch(
			mergeBranchName);

		centralGitWorkingDirectory.checkoutLocalGitBranch(mergeLocalGitBranch);

		return mergeLocalGitBranch;
	}

	private static void _deleteStaleBranches(
			GitWorkingDirectory centralGitWorkingDirectory,
			CentralGitSubrepository centralGitSubrepository,
			String mergeBranchName)
		throws IOException {

		GitRemote upstreamGitRemote = centralGitWorkingDirectory.getGitRemote(
			"upstream");

		if (_upstreamRemoteGitBranchNames == null) {
			_upstreamRemoteGitBranchNames =
				centralGitWorkingDirectory.getRemoteGitBranchNames(
					upstreamGitRemote);
		}

		String mergeBranchNamePrefix = mergeBranchName.substring(
			0, mergeBranchName.lastIndexOf("-"));

		for (String upstreamRemoteGitBranchName :
				_upstreamRemoteGitBranchNames) {

			if ((upstreamRemoteGitBranchName.equals(mergeBranchName) &&
				 !centralGitSubrepository.
					 isGitSubrepositoryUpstreamCommitMerged()) ||
				!upstreamRemoteGitBranchName.startsWith(
					mergeBranchNamePrefix)) {

				continue;
			}

			centralGitWorkingDirectory.deleteRemoteGitBranch(
				upstreamRemoteGitBranchName, upstreamGitRemote);
		}
	}

	private static String _getCiMergeFilePath(
			GitWorkingDirectory centralGitWorkingDirectory, File gitrepoFile)
		throws IOException {

		File centralWorkingDirectory =
			centralGitWorkingDirectory.getWorkingDirectory();

		String ciMergeFilePath = JenkinsResultsParserUtil.getCanonicalPath(
			gitrepoFile);

		ciMergeFilePath = ciMergeFilePath.replace(".gitrepo", "ci-merge");

		return ciMergeFilePath.replace(
			JenkinsResultsParserUtil.combine(
				JenkinsResultsParserUtil.getCanonicalPath(
					centralWorkingDirectory),
				File.separator),
			"");
	}

	private static String _getMergeBranchName(
		String centralUpstreamBranchName, String gitSubrepositoryName,
		String gitSubrepositoryUpstreamCommit) {

		return JenkinsResultsParserUtil.combine(
			"ci-merge-", gitSubrepositoryName, "-", centralUpstreamBranchName,
			"-", gitSubrepositoryUpstreamCommit);
	}

	private static Properties _getPropertiesFromGitrepoFile(File gitrepoFile)
		throws IOException {

		Properties properties = new Properties();

		properties.load(new FileInputStream(gitrepoFile));

		return properties;
	}

	private static void _pushMergeLocalGitBranchToRemote(
		GitWorkingDirectory centralGitWorkingDirectory,
		LocalGitBranch mergeLocalGitBranch, String senderUserName) {

		String centralGitRepositoryName =
			centralGitWorkingDirectory.getGitRepositoryName();

		String originRemoteURL = JenkinsResultsParserUtil.combine(
			"git@github.com:", senderUserName, "/", centralGitRepositoryName,
			".git");

		GitRemote originGitRemote = centralGitWorkingDirectory.addGitRemote(
			true, "tempRemote", originRemoteURL);

		try {
			centralGitWorkingDirectory.pushToRemoteGitRepository(
				false, mergeLocalGitBranch, mergeLocalGitBranch.getName(),
				originGitRemote);
		}
		finally {
			centralGitWorkingDirectory.removeGitRemote(originGitRemote);
		}
	}

	private static final Pattern _githubRemotePattern = Pattern.compile(
		"git@github.com:[-\\w]+\\/(?<gitSubrepositoryName>[-\\w]+)\\.git");
	private static JSONArray _pullsJSONArray;
	private static List<String> _upstreamRemoteGitBranchNames;

}