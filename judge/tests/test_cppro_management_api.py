import json
from io import BytesIO
from zipfile import ZipFile

from django.apps import apps
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from judge.models import Comment, CommentLock, Language, MiscConfig, Submission, SubmissionTestCase
from judge.models.tests.util import CommonDataMixin, create_contest, create_contest_participation, \
    create_contest_problem, create_problem
from judge.views.cppro_api import CPPRO_PROBLEM_COMMENT_REACTIONS_KEY, CPPRO_SUBMISSION_VERIFICATION_SESSION_KEY


def testcase_zip(**files):
    buffer = BytesIO()
    with ZipFile(buffer, 'w') as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return SimpleUploadedFile('testcases.zip', buffer.getvalue(), content_type='application/zip')


class CpproManagementApiTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.problem = create_problem(code='cppro_management_api', is_public=True)
        cls.staff = cls.users['staff_problem_edit_all']
        cls.member = cls.users['normal']

    def test_staff_can_import_and_view_protected_testcase_content(self):
        self.client.force_login(self.staff)
        upload = testcase_zip(**{
            'tests/01.in': '1 2\n',
            'tests/01.out': '3\n',
            'tests/02.in': '10 20\n',
            'tests/02.out': '30\n',
        })
        imported = self.client.post(
            '/api/cppro/problems/%d/testcases/import?mode=replace' % self.problem.id,
            {'file': upload},
        )

        self.assertEqual(imported.status_code, 200)
        self.assertEqual(imported.json()['importedCount'], 2)
        self.assertEqual(imported.json()['totalCount'], 2)
        self.assertEqual(len(imported.json()['testcases']), 2)
        self.assertEqual(imported.json()['testcases'][0]['input_file'], 'tests/01.in')
        self.assertEqual(imported.json()['testcases'][0]['output_file'], 'tests/01.out')
        self.assertNotIn('input', imported.json()['testcases'][0])
        self.assertNotIn('output', imported.json()['testcases'][0])

        details = self.client.get('/api/cppro/problems/%d/testcases' % self.problem.id)
        self.assertEqual(details.status_code, 200)
        rows = details.json()['testcases']
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['input'], '1 2\n')
        self.assertEqual(rows[0]['output'], '3\n')

        submission = Submission.objects.create(
            user=self.member.profile,
            problem=self.problem,
            language=Language.get_python3(),
            status='D',
            result='WA',
            case_total=2,
        )
        SubmissionTestCase.objects.create(
            submission=submission,
            case=0,
            status='WA',
            points=0,
            total=1,
            output='4\n',
        )
        judged = self.client.get('/api/cppro/submissions/%d?includeTests=all&admin=1' % submission.id)
        self.assertEqual(judged.status_code, 200)
        self.assertEqual(judged.json()['testCases'][0]['input'], '1 2\n')
        self.assertEqual(judged.json()['testCases'][0]['output'], '3\n')
        self.assertEqual(judged.json()['testCases'][0]['actual'], '4\n')

        self.client.force_login(self.member)
        own_submission = self.client.get('/api/cppro/submissions/%d?includeTests=all' % submission.id)
        self.assertEqual(own_submission.status_code, 200)
        self.assertEqual(own_submission.json()['testCases'][0]['input'], '')
        self.assertEqual(own_submission.json()['testCases'][0]['output'], '')
        self.assertEqual(self.client.get('/api/cppro/problems/%d/testcases' % self.problem.id).status_code, 403)

    def test_problem_package_inspection_and_download_routes_exist_for_management(self):
        self.client.force_login(self.staff)
        upload = testcase_zip(**{
            'problem.json': json.dumps({'externalId': 'PACK01', 'title': 'Package problem', 'timeLimit': 1500}),
            'statement.md': '# Package problem\n\nSolve it.',
            '01.in': '4\n',
            '01.out': '16\n',
        })
        inspected = self.client.post('/api/cppro/problems/package/inspect', {'file': upload})

        self.assertEqual(inspected.status_code, 200)
        self.assertEqual(inspected.json()['draft']['title'], 'Package problem')
        self.assertEqual(inspected.json()['draft']['testCases'][0]['output'], '16\n')

        downloaded = self.client.get('/api/cppro/problems/%d/package.zip' % self.problem.id)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded['Content-Type'], 'application/zip')

    def test_submission_verification_is_server_side_and_one_time(self):
        self.client.force_login(self.member)
        issued = self.client.post('/api/cppro/submissions/verification-challenge', data='{}', content_type='application/json')

        self.assertEqual(issued.status_code, 201)
        challenge = issued.json()
        self.assertIn('challengeId', challenge)
        self.assertIn('prompt', challenge)
        self.assertNotIn('answer', challenge)
        answer = self.client.session[CPPRO_SUBMISSION_VERIFICATION_SESSION_KEY]['answer']

        wrong = self.client.post(
            '/api/cppro/submissions',
            data=json.dumps({
                'problemId': self.problem.id,
                'language': Language.get_python3().key,
                'code': 'print(3)',
                'verificationChallengeId': challenge['challengeId'],
                'verificationAnswer': answer + 1,
            }),
            content_type='application/json',
        )
        self.assertEqual(wrong.status_code, 400)
        self.assertNotIn(CPPRO_SUBMISSION_VERIFICATION_SESSION_KEY, self.client.session)

        fresh = self.client.post('/api/cppro/submissions/verification-challenge', data='{}', content_type='application/json')
        fresh_challenge = fresh.json()
        fresh_answer = self.client.session[CPPRO_SUBMISSION_VERIFICATION_SESSION_KEY]['answer']
        submitted = self.client.post(
            '/api/cppro/submissions',
            data=json.dumps({
                'problemId': self.problem.id,
                'language': Language.get_python3().key,
                'code': 'print(3)',
                'verificationChallengeId': fresh_challenge['challengeId'],
                'verificationAnswer': fresh_answer,
            }),
            content_type='application/json',
        )
        self.assertIn(submitted.status_code, {201, 202})
        self.assertEqual(Submission.objects.filter(problem=self.problem, user=self.member.profile).count(), 1)

    def test_admin_can_clear_only_explicit_badge_records(self):
        Badge = apps.get_model('judge', 'Badge')
        first = Badge.objects.create(name='Temporary one', mini='one', full_size='one')
        second = Badge.objects.create(name='Temporary two', mini='two', full_size='two')
        self.member.profile.badges.add(first, second)
        self.client.force_login(self.staff)

        cleared = self.client.delete('/api/cppro/admin/badges')

        self.assertEqual(cleared.status_code, 200)
        self.assertGreaterEqual(cleared.json()['deleted'], 2)
        self.assertFalse(Badge.objects.exists())
        self.assertFalse(self.member.profile.badges.exists())

    def test_problem_submission_stats_route_exists_and_uses_visible_submissions(self):
        # DMOJ scales testcase points to the problem's configured points. A
        # fully accepted submission can therefore have points < case_total.
        self.problem.points = 25
        self.problem.save(update_fields=['points'])
        Submission.objects.create(
            user=self.member.profile,
            problem=self.problem,
            language=Language.get_python3(),
            status='D',
            result='AC',
            points=25,
            case_points=100,
            case_total=100,
            time=0.012,
        )
        Submission.objects.create(
            user=self.staff.profile,
            problem=self.problem,
            language=Language.get_python3(),
            status='D',
            result='WA',
            points=0,
            case_points=0,
            case_total=100,
            time=0.024,
        )

        response = self.client.get(
            '/api/cppro/problems/%d/submissions?limit=1&withCount=true' % self.problem.id,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['total'], 2)
        self.assertEqual(len(payload['rows']), 1)
        self.assertEqual(payload['summary']['accepted'], 1)
        self.assertEqual(payload['summary']['partial'], 0)
        self.assertEqual(payload['summary']['wrong_answer'], 1)
        self.assertEqual(payload['summary']['avg_runtime'], 18)
        self.assertEqual(payload['summary']['fastest_ac_runtime'], 12)

        private_problem = create_problem(code='cppro_private_stats', is_public=False)
        self.assertEqual(
            self.client.get('/api/cppro/problems/%d/submissions' % private_problem.id).status_code,
            404,
        )

    def test_problem_comment_routes_use_native_visibility_locks_and_reactions(self):
        public_comments = self.client.get('/api/cppro/problems/%s/comments' % self.problem.code)
        self.assertEqual(public_comments.status_code, 200)
        self.assertEqual(public_comments.json()['rows'], [])

        self.client.force_login(self.member)
        new_user = self.client.post(
            '/api/cppro/problems/%s/comments' % self.problem.code,
            data=json.dumps({'body': 'First comment'}),
            content_type='application/json',
        )
        self.assertEqual(new_user.status_code, 403)

        self.client.force_login(self.staff)
        created = self.client.post(
            '/api/cppro/problems/%s/comments' % self.problem.code,
            data=json.dumps({'body': 'A native DMOJ problem comment'}),
            content_type='application/json',
        )
        self.assertEqual(created.status_code, 201)
        comment = created.json()
        self.assertEqual(comment['author_username'], self.staff.username)
        self.assertEqual(comment['body'], 'A native DMOJ problem comment')

        self.client.force_login(self.users['superuser'])
        reacted = self.client.put(
            '/api/cppro/problems/%s/comments/%s/reaction' % (self.problem.code, comment['id']),
            data=json.dumps({'reaction': 'love'}),
            content_type='application/json',
        )
        self.assertEqual(reacted.status_code, 200)
        self.assertEqual(reacted.json()['reactions'], {'love': 1})
        self.assertEqual(reacted.json()['my_reaction'], 'love')

        self.client.force_login(self.staff)
        CommentLock.objects.create(page='p:%s' % self.problem.code)
        locked = self.client.post(
            '/api/cppro/problems/%s/comments' % self.problem.code,
            data=json.dumps({'body': 'This must be rejected'}),
            content_type='application/json',
        )
        self.assertEqual(locked.status_code, 403)

        private_problem = create_problem(code='cppro_private_comments', is_public=False)
        self.client.logout()
        self.assertEqual(
            self.client.get('/api/cppro/problems/%s/comments' % private_problem.code).status_code,
            404,
        )

    def test_problem_comments_are_disabled_during_native_clarification_contest(self):
        contest = create_contest(key='cppro_comment_clarifications', use_clarifications=True)
        create_contest_problem(contest=contest, problem=self.problem)
        participation = create_contest_participation(contest=contest, user=self.staff.profile)
        self.staff.profile.current_contest = participation
        self.staff.profile.save(update_fields=['current_contest'])
        self.client.force_login(self.staff)

        response = self.client.post(
            '/api/cppro/problems/%s/comments' % self.problem.code,
            data=json.dumps({'body': 'This must use a clarification instead'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 403)
        self.assertIn('clarifications', response.json()['message'])
        self.assertFalse(Comment.objects.filter(body='This must use a clarification instead').exists())

    def test_problem_comment_reactions_update_the_locked_latest_config_row(self):
        comment = Comment.objects.create(
            author=self.staff.profile,
            page='p:%s' % self.problem.code,
            body='Reaction locking test',
        )
        older = MiscConfig.objects.create(
            key=CPPRO_PROBLEM_COMMENT_REACTIONS_KEY,
            value=json.dumps({'comments': {}}),
        )
        latest = MiscConfig.objects.create(
            key=CPPRO_PROBLEM_COMMENT_REACTIONS_KEY,
            value=json.dumps({
                'comments': {
                    str(comment.id): {str(self.users['superuser'].profile.id): 'love'},
                },
            }),
        )
        reactor = self.users['staff_problem_edit_own']
        self.client.force_login(reactor)

        response = self.client.put(
            '/api/cppro/problems/%s/comments/%s/reaction' % (self.problem.code, comment.id),
            data=json.dumps({'reaction': 'like'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['reactions'], {'love': 1, 'like': 1})
        older.refresh_from_db()
        latest.refresh_from_db()
        latest_members = json.loads(latest.value)['comments'][str(comment.id)]
        self.assertEqual(latest_members[str(reactor.profile.id)], 'like')
        self.assertEqual(json.loads(older.value), {'comments': {}})
