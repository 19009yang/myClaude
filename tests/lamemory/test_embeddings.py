import unittest
from types import SimpleNamespace as NS
from core.lamemory.embeddings import EmbeddingConfig, OpenAIEmbeddingProvider


class EmbeddingTests(unittest.TestCase):
    def config(self, **kwargs):
        return EmbeddingConfig('test', 'http://localhost:123/v1', 2, 'v1', **kwargs)
    def test_protocol(self):
        calls = []
        def create(**kwargs):
            calls.append(kwargs)
            return NS(data=[NS(index=1, embedding=[0, 2]), NS(index=0, embedding=[2, 0])])
        provider = OpenAIEmbeddingProvider(self.config(), client=NS(embeddings=NS(create=create)))
        self.assertEqual(provider.embed_documents(['a', 'b'], timeout=3), [[1, 0], [0, 1]])
        self.assertEqual(calls[0]['timeout'], 3)
        self.assertNotIn('dimensions', calls[0])
        self.assertEqual(calls[0]['encoding_format'], 'float')
        self.assertEqual(self.config(api_key='secret').descriptor.space_id, self.config().descriptor.space_id)
        self.assertNotEqual(self.config().descriptor.space_id, EmbeddingConfig('test', 'http://localhost:123/v1', 2, 'v2').descriptor.space_id)
    def test_bad_responses(self):
        for vector in ([1], [0, 0], [True, 1], [float('nan'), 0], [float('inf'), 1]):
            provider = OpenAIEmbeddingProvider(self.config(), client=NS(embeddings=NS(create=lambda **kw: NS(data=[NS(index=0, embedding=vector)]))))
            with self.assertRaises(RuntimeError):
                provider.embed_query('q', timeout=1)
        for indices in ([0, 0], [1], [True, 1]):
            provider = OpenAIEmbeddingProvider(self.config(), client=NS(embeddings=NS(create=lambda **kw: NS(data=[NS(index=i, embedding=[1, 0]) for i in indices]))))
            with self.assertRaises(RuntimeError):
                provider.embed_documents(['a', 'b'], timeout=1)
    def test_redacted_error(self):
        def fail(**kw):
            raise TimeoutError('api-key=secret')
        p = OpenAIEmbeddingProvider(self.config(), client=NS(embeddings=NS(create=fail)))
        with self.assertRaisesRegex(RuntimeError, '^embedding_TimeoutError$'):
            p.embed_query('q', timeout=1)
