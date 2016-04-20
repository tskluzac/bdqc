
"""
This module aggregates all statistics generated by "within-file" analysis,
flattening the JSON data into a matrix.

It then applies the heuristics to identify potentially anomalous files:
1. quantitative data is expected to be centrally-distributed, so robust
	outlier detection is applied to quantitative data
2. categorical data and compound data types are expected to be identical
	across files

Aggregation
----------------------------------------------------------------------------

Given either
1. some root directories to scan for .bdqc cache files and/or
2. aggregate JSON file(s)

...scan all the JSON. In both cases (which can be arbitrarily interleaved)
scanning consumes one file's JSON at a time.

Complex data types
----------------------------------------------------------------------------

The process of flattening can itself be adjusted, particularly with respect
to its treatment of complex data types. TODO: explore this.

Column selectors:
	path match, e.g.
		"/x/y/[p,q-r]/z,<type>
"""

import sys

from bdqc.statistic import Descriptor
from bdqc.column import Vector
from bdqc.report import HTML

# Warning: the values of these constants are used to index function pointer
# arrays. Don't change these without changing code that uses them!
STATUS_NOTHING_TO_SEE = 0
STATUS_MISSING_VALUES = 1
STATUS_MULTIPLE_TYPES = 2
STATUS_ANOMALIES      = 3

STATUS = [
	"no anomalies detected",
	"missing values present in plugin output",
	"conflicts in the column types",
	"anomalies detected"
]

# Matrices may be traverse:
# 1. always
# 2. only if they have scalar component type
# 3. only if they have object component type
# 4. or never.
_ALWAYS_TRAVERSE = True
_EMPTY_MATRIX_TYPE = ((0,),None)

# These are the only additional constraints we put on plugin-generated JSON.
_MSG_BAD_ARRAY  = "Arrays must constitute matrices (N complete dimensions with uniform component type)"
_MSG_BAD_OBJECT = "Objects must not be empty"

if __debug__:
	_is_scalar = lambda v:\
		isinstance(v,int) or \
		isinstance(v,float) or \
		isinstance(v,str)

def _json_matrix_type( l ):
	"""
	Returns a pair (dimensions,element_type) where 
	1. dimensions is a tuple of integers giving each dimension's size, and
	2. element_type is an integer type code.
	For example, if f is a float in...

		[ [[f,f,f,f],[f,f,f,f]],
		  [[f,f,f,f],[f,f,f,f]],
		  [[f,f,f,f],[f,f,f,f]] ]

	...then the return is...

	( (3,2,4), Descriptor.TY_FLOAT )

	Note that TY_NONSCALAR is a legitimate element type, so...
		[ [ {...}, {...}, {...} ],
		  [ {...}, {...}, {...} ],
		  [ {...}, {...}, {...} ],
		  [ {...}, {...}, {...} ] ]

	...is ( (4,3), Descriptor.TY_NONSCALAR )

	Returns None if either:
	1. the matrix is not complete--there is inconsistency in dimensions, or
	2. the element type is not identical throughout.

	Warning: this function is recursive; mind your stack!
	"""
	if not ( isinstance(l,list) and len(l) > 0 ):
		return False
	if all([isinstance(elem,list) for elem in l]):
		types = [ _json_matrix_type(elem) for elem in l ]
		# None percolates up...
		if any([elem is None for elem in types]) or \
			len(frozenset(types)) > 1:
			return None
		else:
			return ( (len(l),)+types[0][0], types[0][1] )
	else: # l is a list that contains AT LEAST ONE non-list
		types = [ Descriptor.scalar_type(elem) for elem in l ]
		# If there are multiple types it's not a matrix at all (by our
		# restrictied definition in this code)
		if len(frozenset(types)) > 1:
			return None
		else:
			return ( (len(types),), types[0] ) 

class Matrix(object):
	"""
	Holds the "flattened" results of the "within-file" analysis carried out
	by plugins on a set of data files.
	Each column corresponds to one of the statistics produced by one of the
	plugins.
	Each row corresponds to an analyzed file.
	Result of analysis is a list of anomalous rows and columns such that:
	1. a column is anomalous if it contains any anomalous row, and
	2. a row is anomalous if its file is anomalous in any statistic (column)
	An incidence matrix can be constructed from these.
	"""
	def __init__( self, config:"a list of column filters"=[] ):
		"""
		"""
		assert isinstance(config,list)
		self.config = config
		# Initialize the column map
		self.column = {}
		self.files  = []
		self.rejects = set()
		# deferred attributes
		# self.status
		# self.anom_col
		# self.anom_row

	def __call__( self, filename, analysis:"JSON data represented as Python objects" ):
		"""
		Process all the (JSON) data generated by the within-file analysis
		of a single primary file.
		Upon exit from this method, each of self.column should be exactly
		one datum longer.

		Returns a boolean indicating whether or not the addition of filename
		resulted in missing values anywhere. (The latest addition might have
		had statistics earlier files didn't, or vica versa.)
		This boolean provides a way to exit file analysis early in (the
		relatively uninteresting) case of missing statistics.
		"""
		# _visit calls self._addstat for each non-traversable element in
		# JSON content. (See elsewhere for what "non-traversable" means.)
		# This will increase the lengths of *some* of self.column by 1.
		columns_on_entry = len(self.column)
		self._visit( analysis )
		self.files.append( filename )
		# Insure all columns have the same length (by inserting missing data
		# placeholders where necessary) before exiting.
		missing_added = 0
		for c in self.column.values():
			if c.pad_to_count( len(self.files) ):
				missing_added += 1
		assert all([ len(c)==len(self.files) for c in self.column.values() ])
		return missing_added > 0 or (
			columns_on_entry > 0 and len(self.column) > columns_on_entry )
 

	def _accept( self, statname ):
		"""
		We only create columns for statistics that match path selectors
		unless any heuristic in use applies to all columns (which most
		of the default do). TODO:revist this.
		"""
		return True

	def _addstat( self, statname, value, meta=None ):
		"""
		Actually write a datum to the appropriate column.
		Called by _visit every time it reaches:
		1. a leaf node (necessarily a scalar) or
		2. a JSON Array (which might represent an arbitrary dimensional
		   matrix or a set).
		The latter case allows:
		1. emission of matrix metadata 
		2. emission of potential sets as values
		...which are mutually exclusive. As long as these possibilities
		*are* mutually exclusive we don't need any additional path
		differentiators--that is, we can just use the JSON path.

		Returns:
			Boolean value for which True means "descend into the matrix."
		(A True is only meaningful if the node is, in fact, a JSON Array).
		"""
		# Following line is preempting fahncy matrix processing.
		# Doing it here rather than tampering with the very complicated
		# _visit method. Let _visit does what it does; we're just not going
		# to act on it now. TODO: revisit this later.
		if meta:
			assert isinstance(value,list)
			return True # descend WITHOUT adding a column
		descend = (meta is not None) and (_ALWAYS_TRAVERSE or meta[1] == 0)
		# Insure a column exists for the statistic...
		try:
			column = self.column[ statname ]
		except KeyError:
			# ...IF AND ONLY IF it:
			# 1. hasn't already been rejected, and
			# 2. passes path-based filters that define stats of interest.
			if statname in self.rejects:
				return descend
			if not self._accept( statname ):
				self.rejects.add( statname )
				return descend # ...without caching anything. 
			# Otherwise, create a new Vector for statname...
			column = Vector(len(self.files))
			self.column[ statname ] = column
		# ...then append the value(s)
		if meta:
			assert isinstance(value,list)
			# If it's a vector (1D matrix) of strings and...
			if len(meta[0]) == 1 and meta[1] == Descriptor.TY_STR:
				S = set(value)
				# ...the cardinality of the set of values equals the count
				# of values, then encode it as a set.
				if len(S) == meta[0][0]:
					column.push( S )
					descend = False
					# ...because the list was just treated as a *value*.
				else:
					column.push( meta )
			else:
				# It's either multi-dimensional or it's element type
				# is Numeric or non-scalar.
				column.push( meta )
		else:
			assert value is None or _is_scalar( value )
			column.push( value )
			descend = False
		return descend

	def _visit( self, data ):
		"""
		Traverse a JSON object, identifying:
		1. scalars (numeric or strings)
		2. matrices
		Matrices are DEFINED as nested Arrays of a single-type (which may itself
		be an Object). Scalar matrices are nested Arrays of a scalar type.
		Nested arrays in which the leaves are not of uniform type are not
		considered matrices.

		Traversal of matrices may be made conditional on the component type.

		One-dimensional matrices are, of course, vectors, and when their content
		is String they may be interpreted as sets.

		The root element (data) is always going to be a JSON Object (Python
		dict) mapping plugin names to the data each plugin produced.
		"""
		assert data and isinstance( data, dict ) # a non-empty dict
		# Using iteration rather than recursion.
		path = []
		stack = []
		node = data
		i = iter(node.items())
		while node:
			push = None
			# Move forward in the current compound value (object or array)
			if isinstance(node,dict): # representing a JSON Object
				try:
					k,v = next(i)
					if isinstance(v,dict):
						if not len(v) > 0:
							raise RuntimeError(_MSG_BAD_OBJECT)
						push = ( k, i )   # NO callback.
					else: # It's a scalar or list, each of which *always*
						# ...trigger a callback.
						pa = '/'.join( path+[k,] )
						if isinstance(v,list):
							# Matrices MAY be empty, but the element type
							# of an empty matrix cannot be determined.
							mt = _json_matrix_type(v) if len(v) > 0 else _EMPTY_MATRIX_TYPE
							if mt is None:
								raise RuntimeError(_MSG_BAD_ARRAY)
							
							# If there are nested Objects (or we're traversing
							# all), descend...
							if self._addstat( pa, v, mt ) and len(v) > 0:
								push = ( k, i )
						else:
							assert v is None or _is_scalar(v)
							self._addstat( pa, v )
				except StopIteration:
					node = None
			else: # node represents a JSON Array
				assert isinstance(node,list) and isinstance(i,int)
				if i < len(node):
					v = node[i]
					if isinstance(v,dict):
						if not len(v) > 0:
							raise RuntimeError(_MSG_BAD_OBJECT)
						push = ( str(i), i+1 ) # NO callback.
					else: # It's a scalar or list, each of which *always*
						# ...trigger a callback.
						pa = '/'.join( path+[str(i),] )
						if isinstance(v,list):
							# Matrices MAY be empty, but the element type
							# of an empty matrix cannot be determined.
							mt = _json_matrix_type(v) if len(v) > 0 else _EMPTY_MATRIX_TYPE
							if mt is None:
								raise RuntimeError(_MSG_BAD_ARRAY)
							
							# If there are nested Objects (or we're traversing
							# all), descend...
							if self._addstat( pa, v, mt ) and len(v) > 0:
								push = ( str(i), i+1 )
							else:
								i += 1
						else:
							assert v is None or _is_scalar(v)
							self._addstat( pa, v )
							i += 1
				else:
					node = None
			# If v is not an empty compound value, a scalar, or a matrix--in
			# other words, if v is further traversable, push the current node
			# onto the stack and start traversing v. (Depth-first traversal.)
			if push:
				assert (isinstance(v,list) or isinstance(v,dict)) and len(v) > 0
				path.append( push[0] )
				stack.append( (node,push[1]) )
				i = iter(v.items()) if isinstance(v,dict) else 0
				node = v
			# If we've exhausted the current node, pop something off the stack
			# to resume traversing. (Clearly, exhaustion is mutually-exclusive
			# of encountering a new tranversable. Thus, the elif...)
			elif (node is None) and stack:
				path.pop(-1)
				node,i = stack.pop(-1)
		# end _visit

	def analyze( self ):
		"""
		1. Identify columns with missing data.
		2. Identify columns for which a single type could not be inferred.
		3. Identify columns with:
			a. multiple values if non-quantitative
			b. outliers if quantitative.

		If no columns meet any of these criteria there is essentially
		nothing to report--everything is normal.

		Missing data implies that the same set of (dynamically determined)
		plugins (or parts of plugins) were not run on all subject files and
		probably indicates inadequately filtered subject files. It is
		impossible in this case to say *which* files (if any) are the source.

		Each column should unambiguously typed as one of the following:
		1. boolean
		2. string
		3. integer
		4. floating-point (possibly with some integers)

		Failure to infer a unique type for a column is probably due to
		either plugin design or flagrant software bugs, and, again, it is
		impossible to identify "guilty" files from this.

		Finally, assuming no data is missing and all columns are uniquely
		typed, each column should contain:
		1. a tightly distributed set of quantitative values (no outliers) or
		2. a single non-quantitative value

		Creates a sparse representation of an incidence matrix.
		"""
		self.anom_col = sorted( list( filter(
			lambda k: self.column[k].is_missing_data(),
			self.column.keys() ) ) )
		if len(self.anom_col) > 0:
			self.anom_row = sorted( list( set().union(*[
				self.column[k].missing_indices() for k in self.anom_col ]) ) )
			self.status = STATUS_MISSING_VALUES
			return self.status

		self.anom_col = sorted( list( filter(
			lambda k: not self.column[k].is_uniquely_typed(),
			self.column.keys() ) ) )
		if len(self.anom_col) > 0:
			self.anom_row = sorted( list( set().union(*[
				self.column[k].minor_type_indices() for k in self.anom_col ]) ) )
			self.status = STATUS_MULTIPLE_TYPES
			return self.status

		# The preceding two cases are the uninteresting cases.

		self.anom_col = sorted( list( filter(
			lambda k: not self.column[k].is_single_valued(),
			self.column.keys() ) ) )

		if len(self.anom_col) > 0:
			# We have a list of column keys (names corresponding to
			# statistics) that are anomalous. Now generate a list of
			# all indices into the self.files list of files that
			# contain anomalies (by virtue of being included in any
			# column's anomaly list).
			self.anom_row = sorted( list( set().union(*[
				self.column[k].outlier_indices() for k in self.anom_col ]) ) )
			self.status = STATUS_ANOMALIES
		else:
			self.status = STATUS_NOTHING_TO_SEE

		return self.status

	def incidence_matrix( self ):
		"""
		Return an incidence matrix the content of which depends on the
		nature of the anomalies (missing data, ambiguous types, or
		value discrepancies).
		"""
		fxn = (
			None,
			Vector.missing_indices,
			Vector.minor_type_indices,
			Vector.outlier_indices)[self.status]
		body = [ [ r in fxn( self.column[c] )
			for c in self.anom_col ]
			for r in self.anom_row ]
		return {
			'body':body,
			'rows':[ self.files[ r ] for r in self.anom_row ],
			'cols':self.anom_col }

class _Loader(object):
	"""
	Matrix is intended to be __call__'ed directly by the run method
	of bdqc.scan.Executor. In particular, it wants a (filename,JSON data)
	pair.
	For the use case where an ad hoc collection of *.bdqc files is
	collected independently via directory recursion, we need a means to
	convert the callbacks from bdqc.dir.walk (that only contain filenames)
	into the required (filename,data) pairs.
	This is the sole purpose of this private class.
	Technically, it is providing a closure.
	"""
	def __init__( self, target, expected_ext=[".bdqc",".json"] ):
		self.target = target
		self.expected_ext = expected_ext

	def _is_valid_filename( self, filename ):
		return any([ filename.endswith(ext) for ext in self.expected_ext ])

	def __call__( self, filename ):
		if not self._is_valid_filename( filename ):
			raise RuntimeError("expected \".bdqc\" cache file, received "+filename )
		with open( filename ) as fp:
			analysis = json.loads( fp )
		basename = os.path.splitext(filename)[0] # snip off the suffix.
		self.target( basename, analysis )


def analyze( args, output ):
	"""
	Aggregate JSON into a Matrix then call the Matrix' analyze method.
	This function allows 
	"""
	import bdqc.dir
	import os.path
	import json

	m = Matrix()
	for s in args.sources:
		if os.path.isdir( s ):
			# Look for "*.bdqc" files under "s/" each of which contains
			# *one* file's analysis as a single JSON object.
			# dir.walk calls a visitor with the filename
			bdqc.dir.walk( s, args.depth, args.include, args.exclude, 
				_Loader( m ) )
		elif os.path.isfile( s ):
			# s is assumed to contain a ("pre") aggregated collection of analyses
			# of multiple files.
			with open(s) as fp:
				for filename,content in json.load( fp ).items():
					m( filename, content )
		else:
			raise RuntimeError( "{} is neither file nor directory".format(s) )
	m.analyze()
	if m.status == STATUS_ANOMALIES:
		HTML(m).render( output )

if __name__=="__main__":
	import argparse

	_parser = argparse.ArgumentParser(
		description="A framework for \"Big Data\" QC/validation.",
		epilog="""The command line interface is one of two ways to use this
		framework. Within your own Python scripts you can create, configure
		and programmatically run a bdqc.Executor.""")

	# Directory recursion options

	_parser.add_argument( "--depth", "-d",
		default=None, type=int,
		help="Maximum depth of recursion when scanning directories.")
	_parser.add_argument( "--include", "-I",
		default=None,
		help="""When recursing through directories, only files matching the
		<include> pattern are included in the analysis. The pattern should
		be a valid Python regular expression and usually should be enclosed
		in single quotes to protect it from interpretation by the shell.""")
	_parser.add_argument( "--exclude", "-E",
		default=None,
		help="""When recursing through directories, any files matching the
		<exclude> pattern are excluded from the analysis. The comments
		regarding the <include> pattern also apply here.""")

	_parser.add_argument( "--config", "-c",
		default=None,
		help="""Load a heuristic configuration from the given file.
		This configuration entirely replaces the defaults.""")

	_parser.add_argument( "sources", nargs="+",
		help="""Files, directories and/or manifests to analyze. All three
		classes of input may be freely intermixed with the caveat that the
		names of manifests (files listing other files) should be prefixed
		by \"@\" on the command line.""" )

	_args = _parser.parse_args()

	# If inclusion or exclusion patterns were specified, just verify they're
	# valid regular expressions here. The result of compilation is discarded
	# now. This is just an opportunity for Python to report a regex format
	# exception before entering directory recursion.

	if _args.include:
		re.compile( _args.include )
	if _args.exclude:
		re.compile( _args.exclude )

	analyze( _args, sys.stdout )

