'''
meta.py

functions for persisting metadata about tables loaded via ETL
'''

from contextlib import contextmanager
import os
import re

from luigi import Task, BooleanParameter, Target

from sqlalchemy import (Column, Integer, String, Boolean, MetaData,
                        create_engine, event, ForeignKey, PrimaryKeyConstraint,
                        ForeignKeyConstraint, Table, exc, func)
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.ext.automap import automap_base
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker, composite, backref
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.orm.collections import attribute_mapped_collection
from sqlalchemy.types import UserDefinedType



def get_engine():
    engine = create_engine('postgres://{user}:{password}@{host}:{port}/{db}'.format(
        user=os.environ.get('PGUSER', 'postgres'),
        password=os.environ.get('PGPASSWORD', ''),
        host=os.environ.get('PGHOST', 'localhost'),
        port=os.environ.get('PGPORT', '5432'),
        db=os.environ.get('PGDATABASE', 'postgres')
    ))

    @event.listens_for(engine, "connect")
    def connect(dbapi_connection, connection_record):
        connection_record.info['pid'] = os.getpid()

    @event.listens_for(engine, "checkout")
    def checkout(dbapi_connection, connection_record, connection_proxy):
        pid = os.getpid()
        if connection_record.info['pid'] != pid:
            connection_record.connection = connection_proxy.connection = None
            raise exc.DisconnectionError(
                "Connection record belongs to pid %s, "
                "attempting to check out in pid %s" %
                (connection_record.info['pid'], pid)
            )
    return engine


metadata = MetaData(get_engine())
Base = declarative_base(metadata=metadata)

Session = sessionmaker(bind=get_engine())


class Geometry(UserDefinedType):
    def get_col_spec(self):
        return "GEOMETRY"

    def bind_expression(self, bindvalue):
        return func.ST_GeomFromText(bindvalue, type_=self)

    def column_expression(self, col):
        return func.ST_AsText(col, type_=self)


# A connection between a table and a column
class BMDColumnTable(Base):

    __tablename__ = 'bmd_column_table'

    column_id = Column(String, ForeignKey('bmd_column.id', ondelete='cascade'), primary_key=True)
    table_id = Column(String, ForeignKey('bmd_table.id', ondelete='cascade'), primary_key=True)

    colname = Column(String, nullable=False)

    column = relationship("BMDColumn", back_populates="tables")
    table = relationship("BMDTable", back_populates="columns")

    extra = Column(JSON)


def targets_creator(coltarget_or_col, reltype):
    if isinstance(coltarget_or_col, BMDColumn):
        col = coltarget_or_col
    else:
        with session_scope() as session:
            with session.no_autoflush:
                col = coltarget_or_col.get(session) or coltarget_or_col._column
    return BMDColumnToColumn(target=col, reltype=reltype)


def sources_creator(coltarget_or_col, reltype):
    if isinstance(coltarget_or_col, BMDColumn):
        col = coltarget_or_col
    else:
        with session_scope() as session:
            with session.no_autoflush:
                col = coltarget_or_col.get(session) or coltarget_or_col._column
    return BMDColumnToColumn(source=col, reltype=reltype)


class BMDColumnToColumn(Base):
    __tablename__ = 'bmd_column_to_column'

    source_id = Column(String, ForeignKey('bmd_column.id', ondelete='cascade'), primary_key=True)
    target_id = Column(String, ForeignKey('bmd_column.id', ondelete='cascade'), primary_key=True)

    reltype = Column(String, primary_key=True)

    source = relationship('BMDColumn',
                          foreign_keys=[source_id],
                          backref=backref(
                              "tgts",
                              collection_class=attribute_mapped_collection("target"),
                              cascade="all, delete-orphan",
                          ))
    target = relationship('BMDColumn',
                          foreign_keys=[target_id],
                          backref=backref(
                              "srcs",
                              collection_class=attribute_mapped_collection("source"),
                              cascade="all, delete-orphan",
                          ))


def tag_creator(tagtarget):
    with session_scope() as session:
        with session.no_autoflush:
            tag = tagtarget.get(session) or tagtarget._tag
            return BMDColumnTag(tag=tag)


# For example, a single census identifier like b01001001
class BMDColumn(Base):
    __tablename__ = 'bmd_column'

    id = Column(String, primary_key=True) # fully-qualified id like '"us.census.acs".b01001001'

    type = Column(String, nullable=False) # postgres type, IE numeric, string, geometry, etc.
    name = Column(String) # human-readable name to provide in bigmetadata

    description = Column(String) # human-readable description to provide in
                                 # bigmetadata

    weight = Column(Integer, default=0)
    aggregate = Column(String) # what aggregate operation to use when adding
                               # these together across geoms: AVG, SUM etc.

    tables = relationship("BMDColumnTable", back_populates="column", cascade="all,delete")
    #tags = relationship("BMDColumnTag", back_populates="column", cascade="all,delete")
    tags = association_proxy('column_tags', 'tag', creator=tag_creator)

    targets = association_proxy('tgts', 'reltype', creator=targets_creator)
    sources = association_proxy('srcs', 'reltype', creator=sources_creator)


# We should have one of these for every table we load in through the ETL
class BMDTable(Base):
    __tablename__ = 'bmd_table'

    id = Column(String, primary_key=True) # fully-qualified id like '"us.census.acs".extract_year_2013_sample_5yr'

    columns = relationship("BMDColumnTable", back_populates="table",
                           cascade="all,delete")

    tablename = Column(String, nullable=False)
    timespan = Column(String)
    bounds = Column(String)
    description = Column(String)


class BMDTag(Base):
    __tablename__ = 'bmd_tag'

    id = Column(String, primary_key=True)

    name = Column(String, nullable=False)
    description = Column(String)

    #columns = relationship("BMDColumnTag", back_populates="tag", cascade="all,delete")
    columns = association_proxy('column_tags', 'column')


class BMDColumnTag(Base):
    __tablename__ = 'bmd_column_tag'

    column_id = Column(String, ForeignKey('bmd_column.id', ondelete='cascade'), primary_key=True)
    tag_id = Column(String, ForeignKey('bmd_tag.id', ondelete='cascade'), primary_key=True)

    column = relationship("BMDColumn",
                          backref=backref('column_tags', cascade='all, delete-orphan')
                         )
    tag = relationship("BMDTag",
                       backref=backref('column_tags', cascade='all, delete-orphan')
                      )


@contextmanager
def session_scope():
    """Provide a transactional scope around a series of operations."""
    session = sessionmaker(bind=get_engine())()
    try:
        yield session
        session.commit()
    except:
        session.rollback()
        raise
    finally:
        session.close()


def fromkeys(d, l):
    '''
    Similar to the builtin dict `fromkeys`, except remove keys with a value
    of None
    '''
    d = d.fromkeys(l)
    return dict((k, v) for k, v in d.iteritems() if v is not None)


Base.metadata.create_all()
