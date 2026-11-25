/**
 * SPDX-FileCopyrightText: (c) 2000 Liferay, Inc. https://liferay.com
 * SPDX-License-Identifier: LGPL-2.1-or-later OR
 * LicenseRef-Liferay-DXP-EULA-2.0.0-2023-06
 */

package com.liferay.change.tracking.web.internal.portlet.action;

import com.liferay.change.tracking.constants.CTConstants;
import com.liferay.change.tracking.constants.CTPortletKeys;
import com.liferay.change.tracking.model.CTCollection;
import com.liferay.change.tracking.model.CTEntry;
import com.liferay.change.tracking.service.CTCollectionLocalService;
import com.liferay.change.tracking.service.CTEntryLocalService;
import com.liferay.change.tracking.spi.display.CTDisplayRenderer;
import com.liferay.change.tracking.spi.display.CTDisplayRendererRegistry;
import com.liferay.change.tracking.web.internal.configuration.CTConfiguration;
import com.liferay.change.tracking.web.internal.display.BasePersistenceRegistry;
import com.liferay.change.tracking.web.internal.display.DisplayContextImpl;
import com.liferay.change.tracking.web.internal.util.PublicationsPortletURLUtil;
import com.liferay.diff.DiffHtml;
import com.liferay.knowledge.base.model.KBArticleModel;
import com.liferay.petra.io.unsync.UnsyncStringWriter;
import com.liferay.petra.lang.SafeCloseable;
import com.liferay.petra.sql.dsl.DSLQueryFactoryUtil;
import com.liferay.petra.string.CharPool;
import com.liferay.petra.string.StringBundler;
import com.liferay.petra.string.StringPool;
import com.liferay.portal.configuration.module.configuration.ConfigurationProvider;
import com.liferay.portal.kernel.change.tracking.CTCollectionThreadLocal;
import com.liferay.portal.kernel.change.tracking.sql.CTSQLModeThreadLocal;
import com.liferay.portal.kernel.comment.CommentManager;
import com.liferay.portal.kernel.comment.Discussion;
import com.liferay.portal.kernel.dao.orm.QueryUtil;
import com.liferay.portal.kernel.exception.PortalException;
import com.liferay.portal.kernel.io.unsync.UnsyncStringReader;
import com.liferay.portal.kernel.json.JSONArray;
import com.liferay.portal.kernel.json.JSONFactory;
import com.liferay.portal.kernel.json.JSONObject;
import com.liferay.portal.kernel.json.JSONUtil;
import com.liferay.portal.kernel.language.Language;
import com.liferay.portal.kernel.log.Log;
import com.liferay.portal.kernel.log.LogFactoryUtil;
import com.liferay.portal.kernel.model.BaseModel;
import com.liferay.portal.kernel.model.GroupedModel;
import com.liferay.portal.kernel.model.Layout;
import com.liferay.portal.kernel.model.Role;
import com.liferay.portal.kernel.model.User;
import com.liferay.portal.kernel.model.WorkflowInstanceLink;
import com.liferay.portal.kernel.portlet.JSONPortletResponseUtil;
import com.liferay.portal.kernel.portlet.LiferayWindowState;
import com.liferay.portal.kernel.portlet.PortletURLFactoryUtil;
import com.liferay.portal.kernel.portlet.bridges.mvc.BaseMVCResourceCommand;
import com.liferay.portal.kernel.portlet.bridges.mvc.MVCResourceCommand;
import com.liferay.portal.kernel.portlet.url.builder.PortletURLBuilder;
import com.liferay.portal.kernel.service.ClassNameLocalService;
import com.liferay.portal.kernel.service.LayoutLocalService;
import com.liferay.portal.kernel.service.RoleLocalService;
import com.liferay.portal.kernel.service.ServiceContext;
import com.liferay.portal.kernel.service.UserLocalService;
import com.liferay.portal.kernel.service.WorkflowInstanceLinkLocalService;
import com.liferay.portal.kernel.servlet.PipingServletResponse;
import com.liferay.portal.kernel.theme.ThemeDisplay;
import com.liferay.portal.kernel.util.ArrayUtil;
import com.liferay.portal.kernel.util.FastDateFormatFactoryUtil;
import com.liferay.portal.kernel.util.GetterUtil;
import com.liferay.portal.kernel.util.LocaleUtil;
import com.liferay.portal.kernel.util.ParamUtil;
import com.liferay.portal.kernel.util.Portal;
import com.liferay.portal.kernel.util.PortletKeys;
import com.liferay.portal.kernel.util.StringUtil;
import com.liferay.portal.kernel.util.Validator;
import com.liferay.portal.kernel.util.WebKeys;
import com.liferay.portal.kernel.workflow.WorkflowConstants;
import com.liferay.portal.kernel.workflow.WorkflowHandler;
import com.liferay.portal.kernel.workflow.WorkflowHandlerRegistryUtil;
import com.liferay.portal.kernel.workflow.WorkflowLog;
import com.liferay.portal.kernel.workflow.WorkflowTask;
import com.liferay.portal.kernel.workflow.WorkflowTaskManager;
import com.liferay.portal.kernel.workflow.WorkflowTaskManagerUtil;
import com.liferay.portal.kernel.workflow.WorkflowTransition;
import com.liferay.portal.workflow.comparator.WorkflowComparatorFactory;
import com.liferay.portal.workflow.manager.WorkflowLogManager;
import com.liferay.segments.model.SegmentsExperience;
import com.liferay.segments.model.SegmentsExperienceModel;
import com.liferay.segments.model.SegmentsExperienceTable;
import com.liferay.segments.service.SegmentsExperienceLocalService;

import jakarta.portlet.ActionRequest;
import jakarta.portlet.PortletRequest;
import jakarta.portlet.ResourceRequest;
import jakarta.portlet.ResourceResponse;

import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;

import java.text.Format;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Comparator;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.function.Function;

import org.osgi.service.component.annotations.Component;
import org.osgi.service.component.annotations.Reference;

/**
 * @author Samuel Trong Tran
 */
@Component(
	property = {
		"jakarta.portlet.name=" + CTPortletKeys.PUBLICATIONS,
		"mvc.command.name=/change_tracking/get_entry_render_data"
	},
	service = MVCResourceCommand.class
)
public class GetEntryRenderDataMVCResourceCommand
	extends BaseMVCResourceCommand {

	@Override
	protected void doServeResource(
			ResourceRequest resourceRequest, ResourceResponse resourceResponse)
		throws Exception {

		try {
			long ctEntryId = ParamUtil.getLong(resourceRequest, "ctEntryId");

			if (ctEntryId > 0) {
				JSONPortletResponseUtil.writeJSON(
					resourceRequest, resourceResponse,
					_getCTEntryRenderDataJSONObject(
						resourceRequest, resourceResponse, ctEntryId));

				return;
			}

			JSONPortletResponseUtil.writeJSON(
				resourceRequest, resourceResponse,
				_getProductionRenderDataJSONObject(
					resourceRequest, resourceResponse));
		}
		catch (PortalException portalException) {
			_log.error(portalException);

			JSONPortletResponseUtil.writeJSON(
				resourceRequest, resourceResponse,
				JSONUtil.put(
					"errorMessage",
					_language.get(
						_portal.getHttpServletRequest(resourceRequest),
						"an-unexpected-error-occurred")));
		}
	}

	private Function<String, ServiceContext> _createServiceContextFunction() {
		return className -> {
			ServiceContext serviceContext = new ServiceContext();

			serviceContext.setWorkflowAction(WorkflowConstants.ACTION_PUBLISH);

			return serviceContext;
		};
	}

	private JSONObject _getEditJSONObject(
		String confirmationMessage, long ctCollectionId, String editURL,
		String label, ResourceRequest resourceRequest,
		ResourceResponse resourceResponse) {

		JSONObject editInProductionJSONObject = JSONUtil.put(
			"editURL", editURL
		).put(
			"label", label
		);

		long activeCTCollectionId = ParamUtil.getLong(
			resourceRequest, "activeCTCollectionId");

		if (activeCTCollectionId != ctCollectionId) {
			editInProductionJSONObject.put(
				"checkoutURL",
				PublicationsPortletURLUtil.getHref(
					resourceResponse.createActionURL(),
					ActionRequest.ACTION_NAME,
					"/change_tracking/checkout_ct_collection", "redirect",
					editURL, "ctCollectionId", String.valueOf(ctCollectionId))
			).put(
				"confirmationMessage", confirmationMessage
			);
		}

		return editInProductionJSONObject;
	}

	private JSONObject _getLocaleJSONObject(String languageId) {
		return JSONUtil.put(
			"label", languageId
		).put(
			"symbol",
			StringUtil.replace(
				StringUtil.toLowerCase(languageId), CharPool.UNDERLINE,
				CharPool.DASH)
		);
	}

	private <T extends BaseModel<T>> JSONObject _getLocalizedPreviewJSONObject(
		String[] availableLanguageIds, long ctCollectionId,
		CTDisplayRenderer<T> ctDisplayRenderer, long ctEntryId,
		CTSQLModeThreadLocal.CTSQLMode ctSQLMode,
		HttpServletRequest httpServletRequest,
		HttpServletResponse httpServletResponse, T model, String type) {

		JSONObject jsonObject = null;

		try (SafeCloseable safeCloseable1 =
				CTCollectionThreadLocal.setCTCollectionIdWithSafeCloseable(
					ctCollectionId);
			SafeCloseable safeCloseable2 =
				CTSQLModeThreadLocal.setCTSQLModeWithSafeCloseable(ctSQLMode)) {

			for (String languageId : availableLanguageIds) {
				String preview = ctDisplayRenderer.renderPreview(
					new DisplayContextImpl<>(
						httpServletRequest, httpServletResponse,
						_classNameLocalService, _ctDisplayRendererRegistry,
						ctEntryId, LocaleUtil.fromLanguageId(languageId), model,
						type));

				if (preview != null) {
					if (jsonObject == null) {
						jsonObject = _jsonFactory.createJSONObject();
					}

					jsonObject.put(languageId, preview);
				}
			}

			return jsonObject;
		}
		catch (Exception exception) {
			_log.error(exception);

			return null;
		}
	}

	private <T extends BaseModel<T>> JSONObject _getLocalizedRenderJSONObject(
			String[] availableLanguageIds,
			HttpServletRequest httpServletRequest,
			HttpServletResponse httpServletResponse, long ctCollectionId,
			CTDisplayRenderer<T> ctDisplayRenderer, long ctEntryId,
			CTSQLModeThreadLocal.CTSQLMode ctSQLMode, T model, String type)
		throws Exception {

		JSONObject jsonObject = _jsonFactory.createJSONObject();

		for (String languageId : availableLanguageIds) {
			jsonObject.put(
				languageId,
				_getRender(
					httpServletRequest, httpServletResponse, ctCollectionId,
					ctDisplayRenderer, ctEntryId, ctSQLMode,
					LocaleUtil.fromLanguageId(languageId), model, type));
		}

		return jsonObject;
	}

	private <T extends BaseModel<T>> String _getPreview(
		long ctCollectionId, CTDisplayRenderer<T> ctDisplayRenderer,
		long ctEntryId, CTSQLModeThreadLocal.CTSQLMode ctSQLMode,
		HttpServletRequest httpServletRequest,
		HttpServletResponse httpServletResponse, Locale locale, T model,
		String type) {

		try (SafeCloseable safeCloseable1 =
				CTCollectionThreadLocal.setCTCollectionIdWithSafeCloseable(
					ctCollectionId);
			SafeCloseable safeCloseable2 =
				CTSQLModeThreadLocal.setCTSQLModeWithSafeCloseable(ctSQLMode)) {

			return ctDisplayRenderer.renderPreview(
				new DisplayContextImpl<>(
					httpServletRequest, httpServletResponse,
					_classNameLocalService, _ctDisplayRendererRegistry,
					ctEntryId, locale, model, type));
		}
		catch (Exception exception) {
			_log.error(exception);

			return null;
		}
	}

	private <T extends BaseModel<T>> JSONObject
			_getProductionRenderDataJSONObject(
				ResourceRequest resourceRequest,
				ResourceResponse resourceResponse)
		throws Exception {

		long modelClassNameId = ParamUtil.getLong(
			resourceRequest, "modelClassNameId");
		long modelClassPK = ParamUtil.getLong(resourceRequest, "modelClassPK");

		T model = _ctDisplayRendererRegistry.fetchCTModel(
			modelClassNameId, modelClassPK);

		if (model == null) {
			model = _basePersistenceRegistry.fetchBaseModel(
				modelClassNameId, modelClassPK);
		}

		HttpServletRequest httpServletRequest = _portal.getHttpServletRequest(
			resourceRequest);

		ThemeDisplay themeDisplay =
			(ThemeDisplay)httpServletRequest.getAttribute(
				WebKeys.THEME_DISPLAY);

		return JSONUtil.put(
			"changeType", "production"
		).put(
			"leftRender",
			_getRender(
				httpServletRequest,
				_portal.getHttpServletResponse(resourceResponse),
				CTConstants.CT_COLLECTION_ID_PRODUCTION,
				_ctDisplayRendererRegistry.getCTDisplayRenderer(
					modelClassNameId),
				0, CTSQLModeThreadLocal.CTSQLMode.DEFAULT,
				themeDisplay.getLocale(), model, CTConstants.TYPE_BEFORE)
		).put(
			"leftTitle", _language.get(httpServletRequest, "production")
		);
	}

	private <T extends BaseModel<T>> String _getRender(
			HttpServletRequest httpServletRequest,
			HttpServletResponse httpServletResponse, long ctCollectionId,
			CTDisplayRenderer<T> ctDisplayRenderer, long ctEntryId,
			CTSQLModeThreadLocal.CTSQLMode ctSQLMode, Locale locale, T model,
			String type)
		throws Exception {

		try (SafeCloseable safeCloseable1 =
				CTCollectionThreadLocal.setCTCollectionIdWithSafeCloseable(
					ctCollectionId);
			SafeCloseable safeCloseable2 =
				CTSQLModeThreadLocal.setCTSQLModeWithSafeCloseable(ctSQLMode);
			UnsyncStringWriter unsyncStringWriter = new UnsyncStringWriter()) {

			ThemeDisplay themeDisplay =
				(ThemeDisplay)httpServletRequest.getAttribute(
					WebKeys.THEME_DISPLAY);

			CTConfiguration ctConfiguration =
				_configurationProvider.getCompanyConfiguration(
					CTConfiguration.class, themeDisplay.getCompanyId());

			httpServletRequest.setAttribute(
				"showAllData", ctConfiguration.showAllData());

			PipingServletResponse pipingServletResponse =
				new PipingServletResponse(
					httpServletResponse, unsyncStringWriter);

			ctDisplayRenderer.render(
				new DisplayContextImpl<>(
					httpServletRequest, pipingServletResponse,
					_classNameLocalService, _ctDisplayRendererRegistry,
					ctEntryId, locale, model, type));

			StringBundler sb = unsyncStringWriter.getStringBundler();

			return sb.toString();
		}
		catch (Exception exception) {
			if (_log.isWarnEnabled()) {
				_log.warn(exception);
			}
		}

		ctDisplayRenderer = _ctDisplayRendererRegistry.getDefaultRenderer();

		try (UnsyncStringWriter unsyncStringWriter = new UnsyncStringWriter()) {
			PipingServletResponse pipingServletResponse =
				new PipingServletResponse(
					httpServletResponse, unsyncStringWriter);

			ctDisplayRenderer.render(
				new DisplayContextImpl<>(
					httpServletRequest, pipingServletResponse,
					_classNameLocalService, _ctDisplayRendererRegistry,
					ctEntryId, locale, model, type));

			StringBundler sb = unsyncStringWriter.getStringBundler();

			return sb.toString();
		}
	}

	private void _getSegmentExperiences(
		CTEntry ctEntry, HttpServletRequest httpServletRequest,
		JSONObject jsonObject) {

		JSONArray jsonArray = _jsonFactory.createJSONArray();

		long plid = ctEntry.getModelClassPK();

		Layout layout = _layoutLocalService.fetchLayout(plid);

		if ((layout != null) && (layout.isDenied() || layout.isPending())) {
			layout = layout.fetchDraftLayout();

			plid = layout.getPlid();
		}

		List<SegmentsExperience> segmentsExperiences = new ArrayList<>(
			_segmentsExperienceLocalService.dslQuery(
				DSLQueryFactoryUtil.select(
					SegmentsExperienceTable.INSTANCE
				).from(
					SegmentsExperienceTable.INSTANCE
				).where(
					SegmentsExperienceTable.INSTANCE.plid.eq(plid)
				)));

		if (segmentsExperiences.isEmpty()) {
			return;
		}

		segmentsExperiences.sort(
			Comparator.comparingInt(SegmentsExperienceModel::getPriority));

		SegmentsExperience highestPrioritySegmentsExperience =
			segmentsExperiences.get(segmentsExperiences.size() - 1);

		long highestPrioritySegmentsExperienceId =
			highestPrioritySegmentsExperience.getSegmentsExperienceId();

		for (SegmentsExperience segmentsExperience : segmentsExperiences) {
			jsonArray.put(
				JSONUtil.put(
					"active",
					() -> {
						if (segmentsExperience.getSegmentsExperienceId() ==
								highestPrioritySegmentsExperienceId) {

							return true;
						}

						return false;
					}
				).put(
					"id", segmentsExperience.getSegmentsExperienceId()
				).put(
					"isDefault", segmentsExperience.isDefault()
				).put(
					"name",
					segmentsExperience.getName(httpServletRequest.getLocale())
				).put(
					"segmentName",
					segmentsExperience.getSegmentsEntryName(
						httpServletRequest.getLocale())
				));

			if (segmentsExperience.getSegmentsExperienceId() ==
					highestPrioritySegmentsExperienceId) {

				jsonObject.put(
					"activeSegmentsExperience",
					jsonArray.get(jsonArray.length() - 1));
			}
		}

		jsonObject.put("segmentsExperiences", jsonArray);
	}

	private <T extends BaseModel<T>> JSONArray _getWorkflowActionsJSONArray(
			CTEntry ctEntry, T model, ThemeDisplay themeDisplay,
			ResourceResponse resourceResponse)
		throws Exception {

		WorkflowTask workflowTask = _getWorkflowTask(ctEntry, model);

		if (workflowTask == null) {
			return null;
		}

		JSONArray jsonArray = _jsonFactory.createJSONArray();

		boolean assignedToUserId = Objects.equals(
			workflowTask.getAssigneeUserId(), themeDisplay.getUserId());

		if ((workflowTask.getAssigneeUserId() == -1) || !assignedToUserId) {
			jsonArray = jsonArray.put(
				JSONUtil.put(
					"href",
					PortletURLBuilder.createRenderURL(
						_portal.getLiferayPortletResponse(resourceResponse),
						PortletKeys.MY_WORKFLOW_TASK
					).setMVCPath(
						"/workflow_task_assign.jsp"
					).setParameter(
						"assigneeUserId", themeDisplay.getUserId()
					).setParameter(
						"assignMode", "assignToMe"
					).setParameter(
						"hideDefaultSuccessMessage", "true"
					).setParameter(
						"workflowTaskId", workflowTask.getWorkflowTaskId()
					).setWindowState(
						LiferayWindowState.POP_UP
					).buildString()
				).put(
					"label",
					_language.get(themeDisplay.getLocale(), "assign-to-me")
				).put(
					"modalHeight", "276px"
				));
		}

		if (assignedToUserId) {
			for (WorkflowTransition workflowTransition :
					WorkflowTaskManagerUtil.getWorkflowTaskWorkflowTransitions(
						workflowTask.getWorkflowTaskId())) {

				jsonArray = jsonArray.put(
					JSONUtil.put(
						"href",
						PortletURLBuilder.createActionURL(
							_portal.getLiferayPortletResponse(resourceResponse),
							PortletKeys.MY_WORKFLOW_TASK
						).setActionName(
							"/portal_workflow_task/complete_task"
						).setRedirect(
							themeDisplay.getURLCurrent()
						).setParameter(
							"assigneeUserId", workflowTask.getAssigneeUserId()
						).setParameter(
							"hideDefaultSuccessMessage", "true"
						).setParameter(
							"transitionName", workflowTransition.getName()
						).setParameter(
							"workflowTaskId", workflowTask.getWorkflowTaskId()
						).buildString()
					).put(
						"label",
						workflowTransition.getLabel(themeDisplay.getLocale())
					).put(
						"namespace",
						_portal.getPortletNamespace(
							PortletKeys.MY_WORKFLOW_TASK)
					));
			}
		}

		jsonArray.put(
			JSONUtil.put(
				"href",
				PortletURLBuilder.createRenderURL(
					_portal.getLiferayPortletResponse(resourceResponse),
					PortletKeys.MY_WORKFLOW_TASK
				).setMVCPath(
					"/workflow_task_assign.jsp"
				).setParameter(
					"assigneeUserId", -1
				).setParameter(
					"assignMode", "assignTo"
				).setParameter(
					"hideDefaultSuccessMessage", "true"
				).setParameter(
					"workflowTaskId", workflowTask.getWorkflowTaskId()
				).setWindowState(
					LiferayWindowState.POP_UP
				).buildString()
			).put(
				"label",
				_language.get(themeDisplay.getLocale(), "assign-to-...")
			).put(
				"modalHeight", "356px"
			));

		return jsonArray;
	}

	private <T extends BaseModel<T>> JSONObject _getWorkflowDataJSONObject(
			CTEntry ctEntry, T model, ResourceResponse resourceResponse,
			ThemeDisplay themeDisplay)
		throws Exception {

		long currentCTCollectionId =
			CTCollectionThreadLocal.getCTCollectionId();

		CTCollection ctCollection = _ctCollectionLocalService.getCTCollection(
			ctEntry.getCtCollectionId());

		long safeCloseableCTCollectionId = ctEntry.getCtCollectionId();

		if (!ctCollection.isInProgress() &&
			(ctCollection.getStatus() != WorkflowConstants.STATUS_EXPIRED) &&
			(ctCollection.getStatus() != WorkflowConstants.STATUS_PENDING) &&
			(ctCollection.getStatus() != WorkflowConstants.STATUS_SCHEDULED)) {

			Map<String, Object> modelAttributes = model.getModelAttributes();

			safeCloseableCTCollectionId = (long)modelAttributes.get(
				"ctCollectionId");
		}

		try (SafeCloseable safeCloseable =
				CTCollectionThreadLocal.setCTCollectionIdWithSafeCloseable(
					safeCloseableCTCollectionId)) {

			WorkflowInstanceLink workflowInstanceLink =
				_getWorkflowInstanceLink(ctEntry, model);

			WorkflowTask workflowTask = _getWorkflowTask(
				workflowInstanceLink, null);

			if (workflowTask == null) {
				return null;
			}

			Format format = FastDateFormatFactoryUtil.getDateTime(
				themeDisplay.getLocale(), themeDisplay.getTimeZone());

			return JSONUtil.put(
				"activities",
				() -> {
					JSONObject workflowLogsJSONObject =
						_jsonFactory.createJSONObject();

					List<WorkflowLog> workflowLogs =
						_workflowLogManager.getWorkflowLogsByWorkflowInstance(
							themeDisplay.getCompanyId(),
							workflowTask.getWorkflowInstanceId(),
							Arrays.asList(
								WorkflowLog.TASK_ASSIGN,
								WorkflowLog.TASK_COMPLETION,
								WorkflowLog.TASK_UPDATE,
								WorkflowLog.TRANSITION),
							QueryUtil.ALL_POS, QueryUtil.ALL_POS,
							_workflowComparatorFactory.
								getLogCreateDateComparator(false));

					for (WorkflowLog workflowLog : workflowLogs) {
						workflowLogsJSONObject.put(
							String.valueOf(workflowLog.getWorkflowLogId()),
							JSONUtil.put(
								"comment",
								_language.get(
									themeDisplay.getRequest(),
									workflowLog.getComment())
							).put(
								"createDate",
								format.format(workflowLog.getCreateDate())
							).put(
								"description",
								_getWorkflowLogDescriptions(
									themeDisplay, workflowLog)
							));
					}

					return workflowLogsJSONObject;
				}
			).put(
				"assignButton",
				() -> {
					if ((currentCTCollectionId !=
							ctEntry.getCtCollectionId()) ||
						workflowTask.isCompleted()) {

						return null;
					}

					return JSONUtil.put(
						"href",
						PortletURLBuilder.createRenderURL(
							_portal.getLiferayPortletResponse(resourceResponse),
							PortletKeys.MY_WORKFLOW_TASK
						).setMVCPath(
							"/workflow_task_assign.jsp"
						).setParameter(
							"assigneeUserId", -1
						).setParameter(
							"assignMode", "assignTo"
						).setParameter(
							"workflowTaskId", workflowTask.getWorkflowTaskId()
						).setWindowState(
							LiferayWindowState.POP_UP
						).buildString()
					).put(
						"label",
						_language.get(themeDisplay.getLocale(), "assign-to-...")
					).put(
						"modalHeight", "356px"
					);
				}
			).put(
				"assignedTo",
				() -> {
					if (!workflowTask.isAssignedToSingleUser()) {
						return _language.get(
							themeDisplay.getLocale(), "nobody");
					}

					return _portal.getUserName(
						workflowTask.getAssigneeUserId(),
						String.valueOf(workflowTask.getAssigneeUserId()));
				}
			).put(
				"comments",
				() -> {
					WorkflowHandler<?> workflowHandler =
						WorkflowHandlerRegistryUtil.getWorkflowHandler(
							workflowInstanceLink.getClassName());

					if (!workflowHandler.isCommentable()) {
						return null;
					}

					JSONObject jsonObject = _jsonFactory.createJSONObject();

					Discussion discussion = _commentManager.getDiscussion(
						themeDisplay.getUserId(),
						workflowInstanceLink.getGroupId(),
						workflowInstanceLink.getClassName(),
						workflowInstanceLink.getClassPK(),
						_createServiceContextFunction());

					int count = discussion.getDiscussionCommentsCount();

					if (count == 1) {
						jsonObject.put(
							"title",
							StringBundler.concat(
								count, " ",
								_language.get(
									themeDisplay.getLocale(), "comment")));
					}
					else {
						jsonObject.put(
							"title",
							StringBundler.concat(
								count, " ",
								_language.get(
									themeDisplay.getLocale(), "comments")));
					}

					HttpServletRequest httpServletRequest =
						themeDisplay.getRequest();

					jsonObject.put(
						"url",
						PortletURLBuilder.createRenderURL(
							_portal.getLiferayPortletResponse(resourceResponse),
							PortletKeys.MY_WORKFLOW_TASK
						).setMVCPath(
							"/edit_workflow_task.jsp"
						).setRedirect(
							PortletURLBuilder.create(
								PortletURLFactoryUtil.create(
									httpServletRequest,
									CTPortletKeys.PUBLICATIONS,
									PortletRequest.RENDER_PHASE)
							).setMVCRenderCommandName(
								"/change_tracking/view_change"
							).setParameter(
								"ctCollectionId", ctEntry.getCtCollectionId()
							).setParameter(
								"ctEntryId", ctEntry.getCtEntryId()
							).buildString()
						).setParameter(
							"workflowTaskId", workflowTask.getWorkflowTaskId()
						).buildString());

					return jsonObject;
				}
			).put(
				"createDate", format.format(workflowTask.getCreateDate())
			).put(
				"dueDate",
				() -> {
					if (workflowTask.getDueDate() != null) {
						return format.format(workflowTask.getDueDate());
					}

					return _language.get(themeDisplay.getLocale(), "never");
				}
			).put(
				"status",
				() -> {
					Map<String, Object> modelAttributes =
						model.getModelAttributes();

					return modelAttributes.get("status");
				}
			).put(
				"taskName", workflowTask.getLabel(themeDisplay.getLocale())
			).put(
				"usages",
				() -> {
					HttpServletRequest httpServletRequest =
						themeDisplay.getRequest();

					return PortletURLBuilder.create(
						PortletURLFactoryUtil.create(
							httpServletRequest, PortletKeys.MY_WORKFLOW_TASK,
							PortletRequest.RENDER_PHASE)
					).setMVCPath(
						"/view_layout_classed_model_usages.jsp"
					).setRedirect(
						PortletURLBuilder.create(
							PortletURLFactoryUtil.create(
								httpServletRequest, CTPortletKeys.PUBLICATIONS,
								PortletRequest.RENDER_PHASE)
						).setMVCRenderCommandName(
							"/change_tracking/view_change"
						).setParameter(
							"ctCollectionId", ctEntry.getCtCollectionId()
						).setParameter(
							"ctEntryId", ctEntry.getCtEntryId()
						).buildString()
					).setParameter(
						"className", workflowInstanceLink.getClassName()
					).setParameter(
						"classPK", workflowInstanceLink.getClassPK()
					).setParameter(
						"workflowTaskId", workflowTask.getWorkflowTaskId()
					).buildString();
				}
			);
		}
	}

	private <T extends BaseModel<T>> WorkflowInstanceLink
			_getWorkflowInstanceLink(CTEntry ctEntry, T model)
		throws Exception {

		long groupId = 0;

		if (model instanceof GroupedModel) {
			GroupedModel groupedModel = (GroupedModel)model;

			groupId = groupedModel.getGroupId();
		}

		long classPK = ctEntry.getModelClassPK();

		if (model instanceof KBArticleModel) {
			Map<String, Object> modelAttributes = model.getModelAttributes();

			classPK = GetterUtil.getLong(
				modelAttributes.get("resourcePrimKey"));
		}

		return _workflowInstanceLinkLocalService.fetchWorkflowInstanceLink(
			ctEntry.getCompanyId(), groupId,
			_portal.getClassName(ctEntry.getModelClassNameId()), classPK);
	}

	private String _getWorkflowLogDescriptions(
			ThemeDisplay themeDisplay, WorkflowLog workflowLog)
		throws PortalException {

		if (workflowLog.getType() == WorkflowLog.TASK_COMPLETION) {
			return _language.format(
				themeDisplay.getLocale(), "x-completed-the-task-x",
				new Object[] {
					_portal.getUserName(
						workflowLog.getAuditUserId(),
						String.valueOf(workflowLog.getAuditUserId())),
					workflowLog.getCurrentWorkflowNodeLabel(
						themeDisplay.getLocale())
				},
				false);
		}
		else if (workflowLog.getType() == WorkflowLog.TASK_UPDATE) {
			return _language.format(
				themeDisplay.getLocale(), "x-updated-the-due-date",
				_portal.getUserName(
					workflowLog.getAuditUserId(),
					String.valueOf(workflowLog.getAuditUserId())),
				false);
		}
		else if (workflowLog.getType() == WorkflowLog.TRANSITION) {
			return _language.format(
				themeDisplay.getLocale(), "x-changed-the-state-from-x-to-x",
				new Object[] {
					_portal.getUserName(
						workflowLog.getAuditUserId(),
						String.valueOf(workflowLog.getAuditUserId())),
					workflowLog.getPreviousWorkflowNodeLabel(
						themeDisplay.getLocale()),
					workflowLog.getCurrentWorkflowNodeLabel(
						themeDisplay.getLocale())
				},
				false);
		}
		else if (workflowLog.getAuditUserId() == workflowLog.getUserId()) {
			User user = _userLocalService.fetchUser(workflowLog.getUserId());

			return _language.format(
				themeDisplay.getLocale(), "x-assigned-the-task-to-x",
				new String[] {
					user.getFullName(), user.isMale() ? "himself" : "herself"
				},
				false);
		}
		else if (workflowLog.getRoleId() == 0) {
			StringBundler sb = new StringBundler(3);

			sb.append(
				_language.format(
					themeDisplay.getLocale(), "x-assigned-the-task-to-x",
					new Object[] {
						_portal.getUserName(
							workflowLog.getAuditUserId(),
							String.valueOf(workflowLog.getAuditUserId())),
						_portal.getUserName(
							workflowLog.getUserId(),
							String.valueOf(workflowLog.getUserId()))
					},
					false));

			if (workflowLog.getPreviousUserId() != 0) {
				sb.append(StringPool.SPACE);

				sb.append(
					_language.format(
						themeDisplay.getLocale(), "previous-assignee-was-x",
						_portal.getUserName(
							workflowLog.getPreviousUserId(),
							String.valueOf(workflowLog.getPreviousUserId())),
						false));
			}

			return sb.toString();
		}

		String actorName = null;

		if (workflowLog.getRoleId() != 0) {
			Role role = _roleLocalService.fetchRole(workflowLog.getRoleId());

			if (role == null) {
				return String.valueOf(workflowLog.getRoleId());
			}

			actorName = role.getName();
		}
		else if (workflowLog.getUserId() != 0) {
			User user = _userLocalService.fetchUser(workflowLog.getUserId());

			if (user == null) {
				return String.valueOf(workflowLog.getUserId());
			}

			actorName = user.getFullName();
		}

		return _language.format(
			themeDisplay.getLocale(), "task-initially-assigned-to-the-x-role",
			String.valueOf(actorName), false);
	}

	private <T extends BaseModel<T>> WorkflowTask _getWorkflowTask(
			CTEntry ctEntry, T model)
		throws Exception {

		return _getWorkflowTask(
			_getWorkflowInstanceLink(ctEntry, model), false);
	}

	private WorkflowTask _getWorkflowTask(
			WorkflowInstanceLink workflowInstanceLink, Boolean completed)
		throws Exception {

		if (workflowInstanceLink == null) {
			return null;
		}

		List<WorkflowTask> workflowTasks =
			_workflowTaskManager.getWorkflowTasksByWorkflowInstance(
				workflowInstanceLink.getCompanyId(), null,
				workflowInstanceLink.getWorkflowInstanceId(), completed, 0, 1,
				null);

		if (workflowTasks.isEmpty()) {
			return null;
		}

		return workflowTasks.get(0);
	}

	private static final Log _log = LogFactoryUtil.getLog(
		GetEntryRenderDataMVCResourceCommand.class);

	@Reference
	private BasePersistenceRegistry _basePersistenceRegistry;

	@Reference
	private ClassNameLocalService _classNameLocalService;

	@Reference
	private CommentManager _commentManager;

	@Reference
	private ConfigurationProvider _configurationProvider;

	@Reference
	private CTCollectionLocalService _ctCollectionLocalService;

	@Reference
	private CTDisplayRendererRegistry _ctDisplayRendererRegistry;

	@Reference
	private CTEntryLocalService _ctEntryLocalService;

	@Reference
	private DiffHtml _diffHtml;

	@Reference
	private JSONFactory _jsonFactory;

	@Reference
	private Language _language;

	@Reference
	private LayoutLocalService _layoutLocalService;

	@Reference
	private Portal _portal;

	@Reference
	private RoleLocalService _roleLocalService;

	@Reference
	private SegmentsExperienceLocalService _segmentsExperienceLocalService;

	@Reference
	private UserLocalService _userLocalService;

	@Reference
	private WorkflowComparatorFactory _workflowComparatorFactory;

	@Reference
	private WorkflowInstanceLinkLocalService _workflowInstanceLinkLocalService;

	@Reference
	private WorkflowLogManager _workflowLogManager;

	@Reference
	private WorkflowTaskManager _workflowTaskManager;

}