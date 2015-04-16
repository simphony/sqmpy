"""
    sqmpy
    ~~~~~

    This file is part of sqmpy project.
"""
__author__ = 'Mehdi Sadeghi'

import os
import unittest
import tempfile

from sqmpy.factory import create_app


class SqmpyLoginTestCase(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_file = tempfile.mkstemp()
        opts = {'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + self.db_file + '?check_same_thread=False',
                'TESTING': True,
                'WTF_CSRF_ENABLED': False,
                'CSRF_ENABLED': False,
                'LOGIN_DISABLED': False}
        app = create_app(**opts)
        self.client = app.test_client()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_file)

    def test_empty_db(self):
        rv = self.client.get('/')
        assert 'You should be redirected automatically to target URL' in rv.data

    def login(self, username, password):
        return self.client.post('/security/login', data=dict(
            username=username,
            password=password
        ), follow_redirects=True)

    def logout(self):
        return self.client.get('/security/logout', follow_redirects=True)

    def test_register(self):
        rv = self.client.get('/security/register')
        for word in ['Register', 'Username', 'Email', 'Password']:
            assert word in rv.data
        rv = self.client.post('/security/register', data=dict(username='admin',
                                                              password='default',
                                                              confirm='default',
                                                              email='abc@abc.com'),
                              follow_redirects=True)
        # We should have been forwarded to login page
        assert '<li class="active"><a href="/">Home</a></li>' in rv.data
        # Logout
        self.logout()
        # Posting the same info should raise an error since the user exists
        rv = self.client.post('/security/register', data=dict(username='admin',
                                                              password='default',
                                                              confirm='default',
                                                              email='abc@abc.com'),
                              follow_redirects=True)
        assert 'User with similar information already exists' in rv.data

    def test_login_logout(self):
        rv = self.client.post('/security/register', data=dict(username='admin',
                                                              password='default',
                                                              confirm='default',
                                                              email='abc@abc.com'),
                              follow_redirects=True)
        # We first logout, because register logged us in
        rv = self.logout()
        # We will be forwarded to login page with a warning message
        assert 'To visit / on this server you need to sign in.' in rv.data
        rv = self.login('adminx', 'default')
        assert 'Invalid username' in rv.data
        rv = self.login('admin', 'default')
        assert 'Successfully logged in' in rv.data

if __name__ == '__main__':
    unittest.main()
