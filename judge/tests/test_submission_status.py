from datetime import timedelta

from django.conf import settings
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from judge.models import Contest, ContestParticipation, ContestProblem, Language, Submission, SubmissionTestCase
from judge.models.problem import ProblemTestcaseResultAccess, SubmissionSourceAccess
from judge.models.tests.util import CommonDataMixin, create_blogpost, create_organization, create_problem, create_user
from judge.views.cppro_api import _register_cppro_post
from judge.views.widgets import csrf_failure


class SubmissionTestcaseStatusAccessTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.problem = create_problem(
            code='hidden_case_status',
            is_public=True,
            testcase_result_visibility_mode=ProblemTestcaseResultAccess.ONLY_SUBMISSION_RESULT,
        )
        cls.submission = Submission.objects.create(
            user=cls.users['normal'].profile,
            problem=cls.problem,
            language=Language.get_python3(),
            status='D',
            result='WA',
            case_points=0,
            case_total=1,
        )
        SubmissionTestCase.objects.create(
            submission=cls.submission,
            case=1,
            status='WA',
            time=0.01,
            memory=1024,
            points=0,
            total=1,
        )
        cls.private_problem = create_problem(
            code='private_submission_detail',
            is_public=False,
            submission_source_visibility_mode=SubmissionSourceAccess.ONLY_OWN,
        )
        cls.private_submission = Submission.objects.create(
            user=cls.users['normal'].profile,
            problem=cls.private_problem,
            language=Language.get_python3(),
            status='D',
            result='WA',
            error='private checker diagnostic',
        )
        cls.users['normal'].email = 'normal@example.test'
        cls.users['normal'].save(update_fields=['email'])

    def test_problem_editor_overrides_hidden_testcase_result_policy(self):
        self.assertTrue(self.problem.is_testcase_result_accessible_by(self.users['superuser']))
        self.assertTrue(self.problem.is_testcase_result_accessible_by(self.users['staff_problem_edit_all']))
        self.assertFalse(self.problem.is_testcase_result_accessible_by(self.users['normal']))

        self.problem.testcase_result_visibility_mode = ProblemTestcaseResultAccess.ALL_TEST_CASE
        self.assertTrue(self.problem.is_testcase_result_accessible_by(self.users['normal']))

    def test_problem_editor_sees_hidden_case_rows_on_detail_and_ajax_fragment(self):
        detail_url = reverse('submission_status', args=[self.submission.id])
        fragment_url = reverse('submission_testcases_query')

        self.client.force_login(self.users['staff_problem_edit_all'])
        detail = self.client.get(detail_url)
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, 'submissions-status-table')
        self.assertContains(detail, 'Test case #1')

        fragment = self.client.get(fragment_url, {'id': self.submission.id})
        self.assertEqual(fragment.status_code, 200)
        self.assertContains(fragment, 'submissions-status-table')
        self.assertContains(fragment, 'Test case #1')

    def test_submission_owner_keeps_hidden_case_rows_hidden(self):
        self.client.force_login(self.users['normal'])
        response = self.client.get(reverse('submission_status', args=[self.submission.id]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'submissions-status-table')

    def test_cppro_problem_manager_can_view_cases_during_frozen_contest(self):
        now = timezone.now()
        contest = Contest.objects.create(
            key='hidden_case_frozen',
            name='Frozen hidden-case contest',
            start_time=now - timedelta(hours=1),
            end_time=now + timedelta(minutes=15),
            frozen_last_minutes=30,
            format_name='icpc',
            is_visible=True,
        )
        ContestProblem.objects.create(contest=contest, problem=self.problem, points=100, order=1)
        self.submission.contest_object = contest
        self.submission.save(update_fields=['contest_object'])
        self.assertTrue(contest.is_frozen)

        manager = create_user(
            username='hidden_case_problem_manager',
            user_permissions=('edit_own_problem',),
        )
        self.problem.authors.add(manager.profile)

        detail_url = '/api/cppro/submissions/%d' % self.submission.id
        self.client.force_login(manager)
        manager_response = self.client.get(detail_url, {'includeTests': 'all', 'admin': '1'})

        self.assertEqual(manager_response.status_code, 200)
        manager_payload = manager_response.json()
        self.assertTrue(manager_payload['can_view_test_details'])
        self.assertEqual(manager_payload['material_access_scope'], 'admin')
        self.assertEqual(len(manager_payload['testCases']), 1)
        self.assertEqual(manager_payload['testCases'][0]['verdict'], 'WA')

        self.client.force_login(self.users['normal'])
        user_response = self.client.get(detail_url, {'includeTests': 'all'})

        self.assertEqual(user_response.status_code, 200)
        user_payload = user_response.json()
        self.assertFalse(user_payload['can_view_test_details'])
        self.assertEqual(user_payload['material_access_scope'], 'contest-restricted')
        self.assertEqual(user_payload['testCases'], [])

    def test_cppro_submission_detail_hides_private_metadata_and_judge_log(self):
        detail_url = '/api/cppro/submissions/%d' % self.private_submission.id

        anonymous_response = self.client.get(detail_url)
        self.assertEqual(anonymous_response.status_code, 404)
        self.assertEqual(anonymous_response.json(), {'message': 'Submission not found.'})

        outsider = create_user(username='private_submission_outsider')
        self.client.force_login(outsider)
        outsider_response = self.client.get(detail_url, {'includeTests': 'all', 'admin': '1'})
        self.assertEqual(outsider_response.status_code, 404)
        self.assertEqual(outsider_response.json(), {'message': 'Submission not found.'})

        self.client.force_login(self.users['normal'])
        owner_response = self.client.get(detail_url)
        self.assertEqual(owner_response.status_code, 200)
        self.assertEqual(owner_response.json()['judge_log'], 'private checker diagnostic')

        internal_error_submission = Submission.objects.create(
            user=self.users['normal'].profile,
            problem=self.problem,
            language=Language.get_python3(),
            status='IE',
            error='internal queue path and infrastructure detail',
        )
        owner_internal_error = self.client.get('/api/cppro/submissions/%d' % internal_error_submission.id)
        self.assertEqual(owner_internal_error.status_code, 200)
        self.assertEqual(owner_internal_error.json()['judge_log'], '')

        self.client.force_login(self.users['staff_problem_edit_all'])
        manager_response = self.client.get(detail_url, {'includeTests': 'all', 'admin': '1'})
        self.assertEqual(manager_response.status_code, 200)
        self.assertEqual(manager_response.json()['judge_log'], 'private checker diagnostic')
        manager_internal_error = self.client.get(
            '/api/cppro/submissions/%d' % internal_error_submission.id,
            {'includeTests': 'all', 'admin': '1'},
        )
        self.assertEqual(manager_internal_error.status_code, 200)
        self.assertEqual(
            manager_internal_error.json()['judge_log'],
            'internal queue path and infrastructure detail',
        )

    def test_cppro_direct_submission_detail_honors_frozen_submission_list(self):
        now = timezone.now()
        public_problem = create_problem(
            code='cppro_frozen_direct_detail',
            is_public=True,
            submission_source_visibility_mode=SubmissionSourceAccess.ALWAYS,
        )
        contest = Contest.objects.create(
            key='cppro_frozen_direct_detail',
            name='Frozen direct detail contest',
            start_time=now - timedelta(hours=1),
            end_time=now + timedelta(minutes=15),
            frozen_last_minutes=30,
            format_name='icpc',
            is_visible=True,
            show_submission_list=True,
        )
        ContestProblem.objects.create(contest=contest, problem=public_problem, points=100, order=1)
        submitter = create_user(username='cppro_frozen_detail_submitter')
        submission = Submission.objects.create(
            user=submitter.profile,
            problem=public_problem,
            language=Language.get_python3(),
            contest_object=contest,
            status='D',
            result='AC',
            points=100,
        )
        outsider = create_user(username='cppro_frozen_detail_outsider')
        self.assertTrue(submission.can_see_detail(outsider))
        self.assertTrue(contest.is_frozen)

        detail_url = '/api/cppro/submissions/%d' % submission.id
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(detail_url).status_code, 404)

        self.client.force_login(submitter)
        self.assertEqual(self.client.get(detail_url).status_code, 200)

        platform_staff = create_user(username='cppro_frozen_detail_staff', is_staff=True)
        self.client.force_login(platform_staff)
        self.assertEqual(self.client.get(detail_url, {'admin': '1'}).status_code, 200)

    def test_cppro_public_data_and_profile_hide_private_submission_and_email(self):
        data_response = self.client.get('/api/cppro/data')
        self.assertEqual(data_response.status_code, 200)
        data_payload = data_response.json()
        submission_ids = {row['id'] for row in data_payload['submissions']}
        self.assertIn(self.submission.id, submission_ids)
        self.assertNotIn(self.private_submission.id, submission_ids)
        normal_row = next(row for row in data_payload['users'] if row['username'] == 'normal')
        self.assertNotIn('email', normal_row)
        self.assertEqual(normal_row['submissions'], 1)

        profile_response = self.client.get('/api/cppro/profile/normal')
        self.assertEqual(profile_response.status_code, 200)
        profile_payload = profile_response.json()
        self.assertNotIn('email', profile_payload['user'])
        self.assertEqual(profile_payload['user']['submissions'], 1)
        recent_ids = {row['id'] for row in profile_payload['recentSubmissions']}
        self.assertIn(self.submission.id, recent_ids)
        self.assertNotIn(self.private_submission.id, recent_ids)

    def test_cppro_private_contest_uses_native_visibility_everywhere(self):
        now = timezone.now()
        contest = Contest.objects.create(
            key='private_cppro_contest',
            name='Private CPPro contest',
            start_time=now - timedelta(minutes=5),
            end_time=now + timedelta(hours=1),
            is_visible=True,
            is_private=True,
        )
        ContestProblem.objects.create(contest=contest, problem=self.problem, points=100, order=1)
        contest.private_contestants.add(self.users['normal'].profile)
        private_contest_submission = Submission.objects.create(
            user=self.users['normal'].profile,
            problem=self.problem,
            language=Language.get_python3(),
            contest_object=contest,
            status='D',
            result='AC',
        )
        self.problem.submission_source_visibility_mode = SubmissionSourceAccess.ALWAYS
        self.problem.save(update_fields=['submission_source_visibility_mode'])

        list_response = self.client.get('/api/cppro/contests')
        self.assertEqual(list_response.status_code, 200)
        self.assertNotIn(contest.id, {row['id'] for row in list_response.json()['rows']})
        self.assertEqual(self.client.get('/api/cppro/contests/%s' % contest.key).status_code, 404)
        self.assertEqual(self.client.get('/api/cppro/contests/%s/standings' % contest.key).status_code, 404)
        self.assertNotIn(contest.id, {row['id'] for row in self.client.get('/api/cppro/data').json()['contests']})
        public_submission_ids = {row['id'] for row in self.client.get('/api/cppro/submissions').json()['rows']}
        self.assertNotIn(private_contest_submission.id, public_submission_ids)

        outsider = create_user(username='cppro_private_contest_outsider')
        self.assertTrue(private_contest_submission.can_see_detail(outsider))
        self.client.force_login(outsider)
        hidden_detail = self.client.get('/api/cppro/submissions/%d' % private_contest_submission.id)
        self.assertEqual(hidden_detail.status_code, 404)

        self.client.force_login(self.users['normal'])
        allowed_list = self.client.get('/api/cppro/contests')
        self.assertIn(contest.id, {row['id'] for row in allowed_list.json()['rows']})
        self.assertEqual(self.client.get('/api/cppro/contests/%s' % contest.key).status_code, 200)
        own_submission_ids = {
            row['id']
            for row in self.client.get('/api/cppro/submissions', {'scope': 'mine'}).json()['rows']
        }
        self.assertIn(private_contest_submission.id, own_submission_ids)
        self.assertEqual(
            self.client.get('/api/cppro/submissions/%d' % private_contest_submission.id).status_code,
            200,
        )

    def test_cppro_active_contest_problems_require_participation(self):
        now = timezone.now()
        contest = Contest.objects.create(
            key='cppro_sealed_active_problems',
            name='Sealed active problems',
            start_time=now - timedelta(minutes=5),
            end_time=now + timedelta(hours=1),
            is_visible=True,
        )
        ContestProblem.objects.create(contest=contest, problem=self.problem, points=100, order=1)

        anonymous_payload = self.client.get('/api/cppro/contests/%s' % contest.key).json()
        self.assertFalse(anonymous_payload['problems_available'])
        self.assertEqual(anonymous_payload['problems'], [])
        self.assertEqual(anonymous_payload['problem_count'], 1)

        participation = ContestParticipation.objects.create(
            contest=contest,
            user=self.users['normal'].profile,
            real_start=now - timedelta(minutes=5),
        )
        self.users['normal'].profile.current_contest = participation
        self.users['normal'].profile.save(update_fields=['current_contest'])
        self.client.force_login(self.users['normal'])
        participant_payload = self.client.get('/api/cppro/contests/%s' % contest.key).json()
        self.assertTrue(participant_payload['problems_available'])
        self.assertEqual([row['id'] for row in participant_payload['problems']], [self.problem.id])

    def test_cppro_join_enforces_closed_registration_window(self):
        now = timezone.now()
        contest = Contest.objects.create(
            key='cppro_registration_required',
            name='Registration required',
            start_time=now - timedelta(minutes=5),
            end_time=now + timedelta(hours=1),
            registration_start=now - timedelta(days=1),
            registration_end=now - timedelta(minutes=10),
            is_visible=True,
        )
        self.client.force_login(self.users['normal'])
        response = self.client.post(
            '/api/cppro/contests/%s/attendance/join' % contest.key,
            data='{}',
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['message'], 'Contest registration is required before joining.')

    def test_cppro_post_social_and_comments_require_native_post_visibility(self):
        hidden_post = create_blogpost(title='hidden_cppro_post', visible=False)
        visible_post = create_blogpost(title='visible_cppro_post', visible=True)
        _register_cppro_post(hidden_post, 'community')
        _register_cppro_post(visible_post, 'community')

        hidden_response = self.client.get('/api/cppro/posts/%s/comments' % hidden_post.id)
        self.assertEqual(hidden_response.status_code, 404)
        visible_response = self.client.get('/api/cppro/posts/%s/comments' % visible_post.id)
        self.assertEqual(visible_response.status_code, 200)

    def test_cppro_data_respects_organization_post_visibility(self):
        organization = create_organization(name='cppro_post_organization', is_unlisted=False)
        organization_post = create_blogpost(
            title='cppro_organization_post',
            visible=True,
            global_post=True,
            organization=organization,
        )
        _register_cppro_post(organization_post, 'community')

        anonymous_posts = {row['id'] for row in self.client.get('/api/cppro/data').json()['posts']}
        self.assertNotIn(organization_post.id, anonymous_posts)

        self.users['normal'].profile.organizations.add(organization)
        self.client.force_login(self.users['normal'])
        member_posts = {row['id'] for row in self.client.get('/api/cppro/data').json()['posts']}
        self.assertIn(organization_post.id, member_posts)

    def test_cppro_contest_detail_honors_hidden_and_frozen_scoreboards(self):
        now = timezone.now()
        frozen_contest = Contest.objects.create(
            key='cppro_frozen_scoreboard',
            name='Frozen CPPro scoreboard',
            start_time=now - timedelta(hours=1),
            end_time=now + timedelta(minutes=15),
            frozen_last_minutes=30,
            format_name='icpc',
            is_visible=True,
        )
        visible_participant = ContestParticipation.objects.create(
            contest=frozen_contest,
            user=self.users['normal'].profile,
            real_start=now - timedelta(hours=1),
            score=97,
            cumtime=970,
            frozen_score=7,
            frozen_cumtime=70,
        )
        other_user = create_user(username='cppro_frozen_other')
        ContestParticipation.objects.create(
            contest=frozen_contest,
            user=other_user.profile,
            real_start=now - timedelta(hours=1),
            score=88,
            cumtime=880,
            frozen_score=8,
            frozen_cumtime=80,
        )
        self.assertTrue(frozen_contest.is_frozen)

        frozen_payload = self.client.get('/api/cppro/contests/%s' % frozen_contest.key).json()
        self.assertTrue(frozen_payload['participant_users_available'])
        frozen_rows = {row['username']: row for row in frozen_payload['participant_users']}
        self.assertEqual(frozen_rows[self.users['normal'].username]['score'], 7)
        self.assertEqual(frozen_rows[self.users['normal'].username]['cumtime'], 70)
        self.assertNotEqual(frozen_rows[self.users['normal'].username]['score'], visible_participant.score)

        standings_payload = self.client.get('/api/cppro/contests/%s/standings' % frozen_contest.key).json()
        standings_rows = {row['username']: row for row in standings_payload['rows']}
        self.assertTrue(standings_payload['frozen'])
        self.assertEqual(standings_rows[self.users['normal'].username]['score'], 7)

        hidden_contest = Contest.objects.create(
            key='cppro_hidden_scoreboard',
            name='Hidden CPPro scoreboard',
            start_time=now - timedelta(hours=1),
            end_time=now + timedelta(hours=1),
            is_visible=True,
            scoreboard_visibility=Contest.SCOREBOARD_HIDDEN,
        )
        ContestParticipation.objects.create(
            contest=hidden_contest,
            user=other_user.profile,
            real_start=now - timedelta(hours=1),
            score=99,
            cumtime=999,
        )
        hidden_payload = self.client.get('/api/cppro/contests/%s' % hidden_contest.key).json()
        self.assertFalse(hidden_payload['participant_users_available'])
        self.assertEqual(hidden_payload['participant_users'], [])

        platform_staff = create_user(username='cppro_scoreboard_platform_staff', is_staff=True)
        self.client.force_login(platform_staff)
        staff_detail_response = self.client.get('/api/cppro/contests/%s' % hidden_contest.key)
        self.assertEqual(staff_detail_response.status_code, 200)
        staff_detail = staff_detail_response.json()
        self.assertTrue(staff_detail['participant_users_available'])
        self.assertEqual(staff_detail['participant_users'][0]['score'], 99)
        staff_standings_response = self.client.get('/api/cppro/contests/%s/standings' % hidden_contest.key)
        self.assertEqual(staff_standings_response.status_code, 200)
        self.assertEqual(staff_standings_response.json()['rows'][0]['score'], 99)

    def test_cppro_public_profiles_and_standings_hide_unlisted_organization_affiliations(self):
        organization = create_organization(name='cppro_unlisted_affiliation', is_unlisted=True)
        member = create_user(username='cppro_unlisted_org_member')
        member.profile.organizations.add(organization)

        data_payload = self.client.get('/api/cppro/data').json()
        data_row = next(row for row in data_payload['users'] if row['username'] == member.username)
        self.assertEqual(data_row['organization_name'], '')
        self.assertEqual(data_row['organization_slug'], '')

        profile_payload = self.client.get('/api/cppro/profile/%s' % member.username).json()
        self.assertEqual(profile_payload['user']['organization_name'], '')
        self.assertEqual(profile_payload['user']['organization_slug'], '')

        now = timezone.now()
        contest = Contest.objects.create(
            key='cppro_unlisted_org_standings',
            name='Unlisted organization standings',
            start_time=now - timedelta(hours=1),
            end_time=now + timedelta(hours=1),
            is_visible=True,
        )
        ContestParticipation.objects.create(
            contest=contest,
            user=member.profile,
            real_start=now - timedelta(hours=1),
            score=10,
            cumtime=100,
        )
        standings_payload = self.client.get('/api/cppro/contests/%s/standings' % contest.key).json()
        standings_row = next(row for row in standings_payload['rows'] if row['username'] == member.username)
        self.assertEqual(standings_row['organization_name'], '')

    def test_cppro_public_organization_redacts_members_and_private_contests(self):
        organization = create_organization(name='cppro_public_org', is_unlisted=False)
        organization.members.add(self.users['normal'].profile)
        hidden_member = create_user(username='cppro_unlisted_member')
        hidden_member.profile.is_unlisted = True
        hidden_member.profile.save(update_fields=['is_unlisted'])
        organization.members.add(hidden_member.profile)
        organization.admins.add(hidden_member.profile)
        now = timezone.now()
        contest = Contest.objects.create(
            key='cppro_org_private_contest',
            name='Organization private contest',
            start_time=now - timedelta(minutes=5),
            end_time=now + timedelta(hours=1),
            is_visible=True,
            is_organization_private=True,
        )
        contest.organizations.add(organization)

        anonymous_response = self.client.get('/api/cppro/organizations/%s' % organization.slug)
        self.assertEqual(anonymous_response.status_code, 200)
        anonymous_payload = anonymous_response.json()
        member_usernames = {row['username'] for row in anonymous_payload['members']}
        self.assertNotIn(hidden_member.username, member_usernames)
        self.assertTrue(all('email' not in row for row in anonymous_payload['members']))
        self.assertNotIn(hidden_member.username, anonymous_payload['organization']['admin_usernames'])
        self.assertNotIn(contest.id, {row['id'] for row in anonymous_payload['contests']})

        self.client.force_login(self.users['normal'])
        member_response = self.client.get('/api/cppro/organizations/%s' % organization.slug)
        self.assertEqual(member_response.status_code, 200)
        self.assertIn(contest.id, {row['id'] for row in member_response.json()['contests']})


class CpproCsrfFailureTestCase(TestCase):
    def test_cppro_auth_me_bootstraps_and_enforces_csrf(self):
        client = Client(enforce_csrf_checks=True)
        bootstrap = client.get('/api/cppro/auth/me')

        self.assertEqual(bootstrap.status_code, 200)
        self.assertIn(settings.CSRF_COOKIE_NAME, bootstrap.cookies)
        csrf_token = bootstrap.cookies[settings.CSRF_COOKIE_NAME].value

        rejected = client.post('/api/cppro/auth/logout', data='{}', content_type='application/json')
        self.assertEqual(rejected.status_code, 403)
        self.assertJSONEqual(rejected.content.decode('utf-8'), {
            'message': 'CSRF token missing or incorrect.',
        })

        accepted = client.post(
            '/api/cppro/auth/logout',
            data='{}',
            content_type='application/json',
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(accepted.status_code, 200)

    def test_cppro_csrf_failure_stays_a_json_403(self):
        request = RequestFactory().post('/api/cppro/submissions')
        response = csrf_failure(request)

        self.assertEqual(response.status_code, 403)
        self.assertJSONEqual(response.content.decode('utf-8'), {
            'message': 'CSRF token missing or incorrect.',
        })

    def test_legacy_csrf_failure_keeps_browser_redirect(self):
        request = RequestFactory().post('/problem/hidden_case_status/edit')
        response = csrf_failure(request)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/problem/hidden_case_status/edit')
